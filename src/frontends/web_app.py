"""Flask frontend for Aime — minimal chat only.

Mirrors `tui_model.py` at the wiring layer: builds an `AgentBackend`, a
`ToolGateway`, and a `ConversationController`, then renders the controller's
`CoreEvent` stream — here, over Server-Sent Events to a single HTML page.

Run from the project's `src/` directory:

    python -m frontends.web_app

then open http://127.0.0.1:5000/.
"""

import os
import re
import sys
import json
import queue
import shutil
import base64
import zipfile
import tempfile
import datetime
import threading
from functools import wraps
from io import StringIO, BytesIO

import requests

# Pillow normalises uploaded images (HEIC, TIFF, BMP, …) into PNG/JPEG so the
# model can consume anything the user attaches. Both are optional: if the
# import fails the /upload endpoint simply falls back to treating files as
# text rather than crashing the whole app.
try:
    from PIL import Image, ImageOps
    try:
        from pillow_heif import register_heif_opener
        register_heif_opener()  # teaches Pillow to open HEIC/HEIF/AVIF
    except Exception:  # noqa: BLE001 - HEIC support is a nice-to-have
        pass
    _PIL_AVAILABLE = True
except Exception:  # noqa: BLE001 - image conversion is best-effort
    _PIL_AVAILABLE = False

from flask import (
    Flask, Response, request, jsonify, session, redirect, g, url_for
)
from rich.console import Console
from rich.markup import Tag, _parse
from rich.style import Style
from rich.text import Span, Text

# Allow `python -m frontends.web_app` from src/ to find provider_backend / aime.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from provider_backend import AnthropicMessagesBackend

from aime import (
    ConversationController,
    CoreEvent,
    ToolGateway,
    CalendarService,
    TopicService,
    config as aime_config,
)
from aime.services import sort_events_by_date
from aime import auth as _auth
from aime import encryption as _enc
from aime import backup as _backup
from aime import email_send as _email_send

from . import stt as _stt

# pypandoc converts markdown → PDF/DOCX/HTML/etc. for topic exports. The
# pypandoc_binary wheel bundles a pandoc binary so it works without any
# system-level install. If the import fails (e.g. wheel unavailable on a
# given platform), the /topics/<id>/export route falls back to markdown-only.
try:
    import pypandoc as _pypandoc
    _PANDOC_AVAILABLE = True
except Exception:  # noqa: BLE001 - export is best-effort
    _pypandoc = None
    _PANDOC_AVAILABLE = False

# WeasyPrint handles HTML → PDF as a pure Python library. Importing it eagerly
# at startup means the (slow) Pango/Cairo initialisation cost is paid once,
# not on the first export request.
try:
    from weasyprint import HTML as _WeasyHTML
    _WEASY_AVAILABLE = True
except Exception:  # noqa: BLE001 - PDF export is best-effort
    _WeasyHTML = None
    _WEASY_AVAILABLE = False


# ---------------------------------------------------------------------------
# Auth wiring (Phase 1: per-route gating, single shared controller still)
# ---------------------------------------------------------------------------

_auth_backend = _auth.LocalAuthBackend(
    os.path.join(aime_config.DATABASE_DIR, "auth.sql")
)
_SECRET_KEY = _auth.load_or_create_secret_key(
    os.path.join(aime_config.DATABASE_DIR, "secret_key")
)
# Per-IP rate limit on /signup. Login lockout is per-account (handled inside
# the auth backend); signup needs an IP-keyed throttle since attackers don't
# pick the account name being limited.
_signup_limiter = _auth.IPRateLimiter(limit=5, window_seconds=60 * 60)


def _user_dir(user_id: int) -> str:
    return os.path.join(aime_config.DATABASE_DIR, "users", str(user_id))


def _conversations_dir(user_id: int) -> str:
    return os.path.join(_user_dir(user_id), "conversations")


# LEGACY MIGRATION — pre-multi-user installs kept all conversations in a
# single shared directory (~/.local/share/aime-assistant/conversations).
# On startup, move any *.json files from there into user 1's per-user
# conversations directory so the first account sees its prior history.
# Idempotent: if the legacy dir is missing or already empty, this is a no-op.
# Safe to remove once all live installs have been upgraded past this version.
def _migrate_legacy_shared_conversations() -> None:
    legacy_dir = os.path.join(
        os.environ.get("HOME", ""), ".local/share/aime-assistant/conversations"
    )
    if not os.path.isdir(legacy_dir):
        return
    try:
        names = [n for n in os.listdir(legacy_dir) if n.endswith(".json")]
    except OSError:
        return
    if not names:
        return
    dest = _conversations_dir(1)
    os.makedirs(dest, exist_ok=True)
    moved = 0
    for name in names:
        src = os.path.join(legacy_dir, name)
        dst = os.path.join(dest, name)
        if os.path.exists(dst):
            continue
        try:
            os.replace(src, dst)
            moved += 1
        except OSError:
            pass
    if moved:
        print(
            f"[migration] moved {moved} legacy conversation(s) from "
            f"{legacy_dir} to {dest}",
            file=sys.stderr,
        )


_migrate_legacy_shared_conversations()
# END LEGACY MIGRATION




# ---------------------------------------------------------------------------
# Per-user controller / SSE state
# ---------------------------------------------------------------------------

_HR_LINE_RE = re.compile(r"(?m)^[ \t]*---[ \t]*$")
# A unicode sentinel the model is overwhelmingly unlikely to produce on its own,
# used to mark horizontal-rule positions through Rich's renderer so we can swap
# them for <hr> in the final HTML.
_HR_SENTINEL = "❦AIMEHR❦"


def _safe_markup_text(markup: str, final: bool = False) -> Text:
    """Forgiving version of `Text.from_markup` — render the markup the model
    *got right* even when part of it is malformed.

    `Text.from_markup` is all-or-nothing: a single stray closing tag, a
    mismatched close, or an unknown style name raises and the whole message
    drops to plain text. That is jarring mid-conversation — the formatting was
    visibly fine while streaming, then a small slip at the end wipes it out.

    Streaming already looks correct because a half-typed message only has
    *unclosed* tags, which Rich tolerates (it closes them implicitly at the
    end). This applies the same forgiveness to every render: stray/unmatched
    closing tags are dropped and unknown style names are shown literally as
    their `[tag]` text.

    For an unclosed opening tag the behaviour depends on `final`:

    * While streaming (`final=False`) it's still open simply because the rest
      of the message hasn't arrived — close it implicitly so the text in
      flight looks styled.
    * In the final render (`final=True`) it's a genuine formatting mistake by
      the model. Rather than guess a closing point and paint half the message
      in a stray colour, drop the tag entirely — the text it wrapped renders
      as plain text. Correctly closed tags elsewhere keep their styling.
    """
    text = Text()
    normalize = Style.normalize
    # Stack of (text_offset, span_style_string, normalized_tag_name).
    style_stack: list[tuple[int, str, str]] = []

    for _position, plain_text, tag in _parse(markup):
        if plain_text is not None:
            # `\[` is an escaped open brace, not the start of a tag.
            text.append(plain_text.replace("\\[", "["))
            continue
        if tag is None:
            continue

        if tag.name.startswith("/"):  # closing tag
            close_name = normalize(tag.name[1:].strip())
            idx: int | None = None
            if close_name:
                for i in range(len(style_stack) - 1, -1, -1):
                    if style_stack[i][2] == close_name:
                        idx = i
                        break
            elif style_stack:  # implicit `[/]`
                idx = len(style_stack) - 1
            if idx is None:
                # Stray closing tag with nothing to match — drop it.
                continue
            # Close this tag plus any tags left open nested inside it.
            while len(style_stack) > idx:
                start, span_style, _name = style_stack.pop()
                if span_style:
                    text.spans.append(Span(start, len(text), span_style))
        else:  # opening tag
            span_style = str(Tag(normalize(tag.name), tag.parameters))
            try:
                Style.parse(span_style)
            except Exception:
                # Unknown style name — show the tag itself as literal text.
                text.append(tag.markup)
                continue
            style_stack.append((len(text), span_style, normalize(tag.name)))

    end = len(text)
    while style_stack:
        start, span_style, _name = style_stack.pop()
        # An unclosed tag in the final render is a model mistake — drop it so
        # its text shows plain. While streaming, close it implicitly instead.
        if span_style and not final:
            text.spans.append(Span(start, end, span_style))
    text.spans.sort(key=lambda span: span.start)
    return text


def _render_markup_to_html(markup: str, final: bool = False) -> str:
    """Convert Rich-style markup to inline-styled HTML spans.

    Lines containing only `---` are treated as horizontal rules — the model
    keeps emitting them as a Markdown reflex even though we render Rich
    markup, so swap them for actual <hr> elements instead of literal dashes.
    """
    markup = _HR_LINE_RE.sub(_HR_SENTINEL, markup)

    console = Console(
        record=True,
        file=StringIO(),
        force_terminal=True,
        color_system="truecolor",
        width=10_000,
    )
    # `_safe_markup_text` repairs malformed markup instead of falling back to
    # plain text, so a near-miss in the model's formatting keeps the parts it
    # got right — both while streaming and in the final render.
    rendered = _safe_markup_text(markup, final=final)
    console.print(rendered, soft_wrap=True, end="")
    html = console.export_html(inline_styles=True, code_format="{code}")
    html = html.replace(_HR_SENTINEL, '<hr class="md-hr">')
    # Collapse runs of blank lines so paragraph separation stays modest under
    # white-space: pre-wrap. Two newlines (one blank line) is the most we ever
    # need visually; longer runs would render as cavernous gaps in the bubble.
    html = re.sub(r"\n{3,}", "\n\n", html)
    return html.strip("\n")


class UserContext:
    """Everything keyed to a single logged-in user: their controller, agent
    backend session, gateway, SSE subscribers, replay history, and streaming
    text buffer. One instance per user is built on demand and cached for the
    lifetime of the process — we don't try to evict yet because controller
    state (conversation history) is expensive to rebuild and personal use
    won't run into pressure."""

    def __init__(self, user_id: int, username: str | None = None):
        self.user_id = user_id
        self.username = username

        conv_dir = _conversations_dir(user_id)
        os.makedirs(conv_dir, exist_ok=True)
        # The DEK is always derivable from the machine secret, no password
        # needed. login_required gates everything that reaches here.
        dek = _auth_backend.get_dek(user_id)

        from aime.model_router import ModelRouter
        from aime.web_search_agent import WebSearchAgent
        from aime import usage as _aime_usage
        router = ModelRouter(
            haiku_model=aime_config.HAIKU_MODEL,
            sonnet_model=aime_config.SONNET_MODEL,
            router_model=aime_config.ROUTER_MODEL,
            enabled=aime_config.MODEL_ROUTING_ENABLED,
            usage_label=username,
            record_api=_aime_usage.record_api,
        )
        web_search_agent = WebSearchAgent(
            model=aime_config.WEB_SEARCH_MODEL,
            tool_version=aime_config.WEB_SEARCH_TOOL_VERSION,
            usage_label=username,
            record_api=_aime_usage.record_api,
        ) if aime_config.WEB_SEARCH_ENABLED else None
        backend = AnthropicMessagesBackend(
            system_prompt=aime_config.load_system_prompt(),
            model=aime_config.AGENT_MODEL,
            schema_files=aime_config.SCHEMA_FILES,
            conversations_dir=conv_dir,
            dek=dek,
            usage_label=username,
            router=router,
            web_search_schema=(
                aime_config.WEB_SEARCH_SCHEMA if aime_config.WEB_SEARCH_ENABLED else None
            ),
            onboarding_tool_schema=aime_config.ONBOARDING_TOOL_SCHEMA,
        )
        backend.new_session()

        # SSE plumbing: one queue per connected client, plus a replayable
        # history for refreshes. Locks are per-user so concurrent users don't
        # serialize against each other. Built before the gateway so the
        # on_mutation callback always has somewhere to broadcast to.
        self._subscribers_lock = threading.Lock()
        self._client_queues: list[queue.Queue] = []
        self._history_lock = threading.Lock()
        self._history: list[dict] = []

        # Streaming assistant text accumulator. Rich-markup tags can span
        # delta boundaries; we render to HTML once a block ends.
        self._assistant_buf: list[str] = []
        self._assistant_buf_lock = threading.Lock()

        gateway = ToolGateway(
            api_url=aime_config.API_URL,
            user_id=user_id,
            on_mutation=self._on_backend_mutation,
        )
        self.gateway = gateway
        self.calendar_service = CalendarService(gateway)
        self.topic_service = TopicService(gateway)

        def spawn_worker(fn):
            threading.Thread(
                target=fn, name=f"agent-{user_id}", daemon=True
            ).start()

        self.controller = ConversationController(
            backend=backend,
            tool_gateway=gateway,
            worker_spawner=spawn_worker,
            web_search_agent=web_search_agent,
        )

        self.controller.subscribe(self._fanout)
        self.controller.start()

        # Tracks records the user has edited via the UI since the model last
        # took a turn. Drained as a compact <stale> tag on the next /send so
        # the model knows its earlier view of those records is out of date.
        # Tuple is (kind, id, title); kind is "topic" or "event".
        self._stale_lock = threading.Lock()
        self._stale_records: list[tuple[str, int, str]] = []

    # ---- Stale-record tracking --------------------------------------------

    def mark_record_stale(self, kind: str, record_id: int, title: str) -> None:
        """Note that the user just edited a record via the UI. Dedupes on
        (kind, id) so repeated edits to the same record only cost one entry."""
        title = (title or "").strip()
        with self._stale_lock:
            self._stale_records = [
                e for e in self._stale_records
                if not (e[0] == kind and e[1] == record_id)
            ]
            self._stale_records.append((kind, record_id, title))

    def drain_stale_tag(self) -> str:
        """Build the <stale> tag for the next user turn, then clear the list.
        Format: `<stale>e23 boxing match;t7 grocery list</stale>` — kind is
        a single letter (e/t) followed by the id, then a space and title.
        Entries are joined with `;` and there is no trailing whitespace, so
        the tag stays as token-cheap as possible. Returns "" when empty."""
        with self._stale_lock:
            if not self._stale_records:
                return ""
            records = self._stale_records
            self._stale_records = []
        parts: list[str] = []
        for kind, rid, title in records:
            tag = "e" if kind == "event" else "t"
            parts.append(f"{tag}{rid} {title}" if title else f"{tag}{rid}")
        return "<stale>" + ";".join(parts) + "</stale>"

    # ---- SSE primitives ---------------------------------------------------

    def attach_client(self) -> tuple[queue.Queue, list[dict]]:
        """Snapshot history and subscribe in one atomic step — under both
        locks together so a concurrent broadcast can't slip an event in
        between the snapshot and the subscribe (which would either lose or
        duplicate it)."""
        q: queue.Queue = queue.Queue(maxsize=8192)
        with self._history_lock, self._subscribers_lock:
            snapshot = list(self._history)
            self._client_queues.append(q)
        return q, snapshot

    def detach_client(self, q: queue.Queue) -> None:
        with self._subscribers_lock:
            if q in self._client_queues:
                self._client_queues.remove(q)

    def _broadcast(self, payload: dict) -> None:
        with self._history_lock:
            if payload.get("kind") == "session_restart":
                self._history.clear()
            # Streaming text deltas and presentational events don't replay —
            # the assistant_html that follows is the final rendered form.
            # `remote_edit` is a transient refresh ping; replaying it on
            # reconnect would just trigger redundant refetches.
            if payload.get("kind") not in (
                "assistant_text_delta", "assistant_text_end",
                "assistant_html_partial", "turn_end", "ready",
                "remote_edit",
            ):
                self._history.append(payload)
        with self._subscribers_lock:
            targets = list(self._client_queues)
        for q in targets:
            try:
                q.put_nowait(payload)
            except queue.Full:
                pass

    def notify_remote_edit(self, source: str) -> None:
        """Tell every connected session of this user to re-fetch their
        topic/calendar views. This is the single refresh-fanout entry point:
        anything that mutates user-visible state (agent tool call via the
        gateway, direct UI endpoint, future background job, etc.) should
        funnel through here so the frontend only needs one handler to wire
        up. `source` is for debugging — it shows up in the SSE payload."""
        self._broadcast({"kind": "remote_edit", "source": source})

    def _on_backend_mutation(self, tool_name: str) -> None:
        # Fired by ToolGateway after any successful non-read tool call,
        # covering both AI tool calls and direct UI service calls.
        self.notify_remote_edit(tool_name)

    def _fanout(self, event: CoreEvent) -> None:
        partial_full: str | None = None
        if event.kind == "assistant_text_delta":
            with self._assistant_buf_lock:
                self._assistant_buf.append(event.text or "")
                partial_full = "".join(self._assistant_buf)
        elif event.kind in ("user_message_shown", "session_restart"):
            with self._assistant_buf_lock:
                self._assistant_buf.clear()

        payload = {
            "kind": event.kind,
            "text": event.text,
            "tool_name": event.tool_name,
            "tool_details": event.tool_details,
            "tool_result_summary": event.tool_result_summary,
            "tool_detail_full": event.tool_detail_full,
            "severity": event.severity,
            "stop_reason": event.stop_reason,
            "from_replay": event.from_replay,
            "attachments": event.attachments,
        }
        self._broadcast(payload)

        if event.kind == "assistant_text_delta" and partial_full:
            self._broadcast({
                "kind": "assistant_html_partial",
                "text": _render_markup_to_html(partial_full),
            })
        elif event.kind == "assistant_text_end":
            with self._assistant_buf_lock:
                full = "".join(self._assistant_buf)
                self._assistant_buf.clear()
            if full:
                self._broadcast({
                    "kind": "assistant_html",
                    "text": _render_markup_to_html(full, final=True),
                })
        elif event.kind == "assistant_text" and event.text:
            self._broadcast({
                "kind": "assistant_html",
                "text": _render_markup_to_html(event.text, final=True),
                "from_replay": event.from_replay,
            })


# Lazy per-user context cache. Built on first request from that user.
_user_contexts: dict[int, UserContext] = {}
_user_contexts_lock = threading.Lock()


def _context_for(user_id: int) -> UserContext:
    """Get (or build) the UserContext for the given user. Building includes
    spinning up an agent backend session, so we serialize on the cache lock
    to avoid two threads racing to construct the same one."""
    ctx = _user_contexts.get(user_id)
    if ctx is not None:
        return ctx
    with _user_contexts_lock:
        ctx = _user_contexts.get(user_id)
        if ctx is None:
            ctx = UserContext(user_id, g.get("username"))
            _user_contexts[user_id] = ctx
        return ctx


# ---------------------------------------------------------------------------
# HTTP app
# ---------------------------------------------------------------------------

app = Flask(__name__)
app.secret_key = _SECRET_KEY

# Reverse-proxy awareness. When AIME_TRUSTED_PROXY_HOPS > 0, ProxyFix rewrites
# request.remote_addr and request.scheme from X-Forwarded-For / -Proto so the
# signup IP rate limiter and the security audit log see the real client IP
# instead of the proxy's. The hop count must equal the number of proxies
# actually in front of Flask — trusting more hops than exist lets a client
# spoof their source IP via a forged header. Default 0 (ignore forwarded
# headers) is the safe value for direct/loopback installs; set to 1 when
# running behind a single reverse proxy (Caddy, nginx, ALB). The docker-compose
# defaults this to 1 because that deployment is documented as behind-a-proxy.
_TRUSTED_PROXY_HOPS = int(os.environ.get("AIME_TRUSTED_PROXY_HOPS", "0"))
if _TRUSTED_PROXY_HOPS > 0:
    from werkzeug.middleware.proxy_fix import ProxyFix
    app.wsgi_app = ProxyFix(
        app.wsgi_app,
        x_for=_TRUSTED_PROXY_HOPS,
        x_proto=_TRUSTED_PROXY_HOPS,
        x_host=_TRUSTED_PROXY_HOPS,
    )
# Session cookie hardening. `Secure` is conditional so local http://127.0.0.1
# development still works; flip on AIME_HTTPS=1 when serving behind TLS.
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Strict",
    SESSION_COOKIE_SECURE=bool(int(os.environ.get("AIME_HTTPS", "0"))),
    PERMANENT_SESSION_LIFETIME=60 * 60 * 24 * 14,  # 14 days
    # Cap request bodies. Attachments (images, audio) flow through /send and
    # /transcribe; 32 MiB is comfortably above realistic use and bounds the
    # damage from a malicious client uploading multi-GB payloads.
    MAX_CONTENT_LENGTH=32 * 1024 * 1024,
)


# Content Security Policy. Inline styles are required (Rich produces inline
# style="..." on every span), and we load marked + DOMPurify from jsdelivr.
# Everything else is locked to same-origin.
_CSP = (
    "default-src 'self'; "
    "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
    "style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data: blob:; "
    "media-src 'self' blob:; "
    "connect-src 'self'; "
    "font-src 'self' data:; "
    "frame-ancestors 'none'; "
    "base-uri 'self'; "
    "form-action 'self'"
)


@app.after_request
def _security_headers(resp):
    resp.headers.setdefault("Content-Security-Policy", _CSP)
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("X-Frame-Options", "DENY")
    resp.headers.setdefault("Referrer-Policy", "same-origin")
    resp.headers.setdefault("Permissions-Policy", "interest-cohort=()")
    return resp


_PAGE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "resources", "style", "web_chat.html",
)
_LOGIN_PAGE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "resources", "style", "login.html",
)
_VERIFY_PAGE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "resources", "style", "verify_code.html",
)
_ADD_EMAIL_PAGE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "resources", "style", "add_email.html",
)


def _load_page() -> str:
    with open(_PAGE_PATH) as f:
        return f.read()


# Account creation is gated by AIME_ALLOW_SIGNUP. Default is off ("0") so a
# self-hosted instance is closed by default: the admin creates accounts, then
# runs with signup disabled so nobody else can register. Set AIME_ALLOW_SIGNUP=1
# to open public registration.
_ALLOW_SIGNUP = bool(int(os.environ.get("AIME_ALLOW_SIGNUP", "0")))

# Access mode — see docs/access-control.md. "keys" (the default) gates /send
# behind each user's api_access flag; new accounts start with no send access
# and must redeem an invite key. "open" disarms the gate entirely. An
# unrecognised value is treated as "keys" so a typo fails closed.
_ACCESS_MODE = os.environ.get("AIME_ACCESS_MODE", "keys").strip().lower()
if _ACCESS_MODE not in ("keys", "open"):
    _ACCESS_MODE = "keys"

# Toggle for the email 2FA flow. Off by default so a fresh install behaves
# like it did before the feature shipped — handy for dev, demos, and anyone
# who hasn't configured EMAIL_ADDRESS / EMAIL_PASSWORD yet. When off:
#   * the Email field on the signup form is hidden,
#   * /signup creates the account directly (no code mailed),
#   * existing accounts without an email are NOT gated on next login.
def _env_bool(name: str, default: str = "0") -> bool:
    return os.environ.get(name, default).strip() not in ("", "0", "false", "False", "no")


_DO_EMAIL_VERIFICATION = _env_bool("DO_EMAIL_VERIFICATION", "0")

# When signup is disabled, hide the "Create account" tab and form on the login
# page so visitors aren't offered something the server will reject.
_SIGNUP_DISABLED_STYLE = (
    '<style>[data-tab="signup"],form[data-form="signup"]{display:none!important}</style>'
)

# When email verification is off, also hide the Email input + its label + its
# helper hint on the signup form. The field stays in the DOM (so the POST still
# carries an empty value, which the server ignores in that mode) but is
# visually removed so the form matches the pre-2FA layout.
_EMAIL_VERIFICATION_DISABLED_STYLE = (
    '<style>'
    'label[for="signup-email"],#signup-email,'
    'label[for="signup-email"] + #signup-email + .hint'
    '{display:none!important}'
    '</style>'
)


def _load_login_page(
    login_error: str = "",
    signup_error: str = "",
    *,
    notice: str = "",
    recover_username: str = "",
    login_username: str = "",
    signup_username: str = "",
    signup_email: str = "",
) -> str:
    """Render the login page.

    `notice` shows an informational line above the sign-in form (e.g. after a
    recovery). `recover_username`, when set, switches the page into its
    account-recovery prompt for that account. `login_username` pre-fills the
    sign-in username field; `signup_username` / `signup_email` pre-fill the
    signup form after a validation error so the user doesn't retype them.
    """
    with open(_LOGIN_PAGE_PATH) as f:
        html = f.read()
    return (
        html
        .replace("__LOGIN_ERROR__", _h(login_error))
        .replace("__SIGNUP_ERROR__", _h(signup_error))
        .replace("__LOGIN_NOTICE__", _h(notice))
        .replace("__RECOVER_USERNAME__", _h(recover_username))
        .replace("__LOGIN_USERNAME__", _h(login_username))
        .replace("__SIGNUP_USERNAME__", _h(signup_username))
        .replace("__SIGNUP_EMAIL__", _h(signup_email))
        .replace("__SIGNUP_DISABLED_STYLE__", "" if _ALLOW_SIGNUP else _SIGNUP_DISABLED_STYLE)
        .replace("__EMAIL_VERIFICATION_DISABLED_STYLE__",
                 "" if _DO_EMAIL_VERIFICATION else _EMAIL_VERIFICATION_DISABLED_STYLE)
        .replace("__EMAIL_REQUIRED__", "required" if _DO_EMAIL_VERIFICATION else "")
    )


def _mask_email(email: str) -> str:
    """Show enough of an email to confirm "yes that's the one I gave you"
    without putting the full address on a page that might be over the user's
    shoulder. Falls back to the raw value if it doesn't look like an email."""
    if not email or "@" not in email:
        return email or ""
    local, _, domain = email.partition("@")
    if len(local) <= 2:
        masked_local = local[:1] + "•"
    else:
        masked_local = local[0] + "•" * (len(local) - 2) + local[-1]
    return f"{masked_local}@{domain}"


def _load_verify_page(
    *,
    email: str,
    verify_action: str,
    resend_action: str,
    cancel_href: str,
    purpose_phrase: str,
    error: str = "",
    notice: str = "",
) -> str:
    with open(_VERIFY_PAGE_PATH) as f:
        html = f.read()
    return (
        html
        .replace("__EMAIL__", _h(_mask_email(email)))
        .replace("__VERIFY_ACTION__", _h(verify_action))
        .replace("__RESEND_ACTION__", _h(resend_action))
        .replace("__CANCEL_HREF__", _h(cancel_href))
        .replace("__PURPOSE_PHRASE__", _h(purpose_phrase))
        .replace("__ERROR__", _h(error))
        .replace("__NOTICE__", _h(notice))
    )


def _load_add_email_page(error: str = "") -> str:
    with open(_ADD_EMAIL_PAGE_PATH) as f:
        html = f.read()
    return html.replace("__ERROR__", _h(error))


def _h(s: str) -> str:
    """Minimal HTML-escape for the small set of fields we interpolate into
    the login page. Avoids pulling in jinja just for two placeholders."""
    return (
        s.replace("&", "&amp;")
         .replace("<", "&lt;")
         .replace(">", "&gt;")
         .replace('"', "&quot;")
         .replace("'", "&#39;")
    )


# ---------------------------------------------------------------------------
# Login-required decorator + auth routes
# ---------------------------------------------------------------------------


def _wants_json() -> bool:
    """True when the client is calling an API endpoint rather than loading
    a page. We answer 401 JSON for these and 302→/login for everything else."""
    if request.is_json:
        return True
    accept = request.headers.get("Accept", "")
    return "application/json" in accept and "text/html" not in accept


def login_required(view):
    @wraps(view)
    def wrapper(*args, **kwargs):
        uid = session.get("user_id")
        if not uid:
            if _wants_json():
                return jsonify({"ok": False, "error": "auth required"}), 401
            return redirect(url_for("login_page"))
        user = _auth_backend.lookup(uid)
        if user is None:
            # Account was deleted but cookie outlived it.
            session.clear()
            if _wants_json():
                return jsonify({"ok": False, "error": "auth required"}), 401
            return redirect(url_for("login_page"))
        g.user_id = user.id
        g.username = user.username
        # api_access gates message sending only; login itself never depends
        # on it, so a user with no send access can still log in and read all
        # their data (topics, calendar, past conversations).
        g.api_access = user.api_access
        return view(*args, **kwargs)
    return wrapper


def api_access_required(view):
    """Gate a route behind the user's send access. Applies *under*
    login_required (which populates g.api_access). In "open" access mode the
    gate is disarmed and this is a pass-through; in "keys" mode a user without
    api_access gets a 403 telling them to redeem an invite key."""
    @wraps(view)
    def wrapper(*args, **kwargs):
        if _ACCESS_MODE == "keys" and not g.get("api_access", False):
            return jsonify({
                "ok": False,
                "error": "no_access",
                "message": "This account doesn't have message access yet. "
                           "Add an invite key in your profile settings.",
            }), 403
        return view(*args, **kwargs)
    return wrapper


@app.route("/login", methods=["GET"])
def login_page():
    if session.get("user_id"):
        return redirect("/")
    # Drop any stale recovery handoff (e.g. the user navigated back here, or
    # chose "No" on the recovery prompt, which links to /login). Same for any
    # half-finished email verification — visiting /login is an explicit
    # restart of the auth flow.
    session.pop("recover_user_id", None)
    stale_signup = session.pop("pending_signup_token", None)
    if stale_signup:
        _auth_backend.cancel_verification(stale_signup)
    stale_email = session.pop("pending_email_token", None)
    if stale_email:
        _auth_backend.cancel_verification(stale_email)
    session.pop("pending_email_user_id", None)
    session.pop("pending_email_was_reinitialized", None)
    return Response(_load_login_page(), mimetype="text/html")


@app.route("/login", methods=["POST"])
def login_submit():
    username = (request.form.get("username") or "").strip()
    password = request.form.get("password") or ""
    try:
        user, _dek, was_reinitialized = _auth_backend.verify(
            username, password, ip=request.remote_addr,
        )
    except _auth.AccountLocked as e:
        return Response(
            _load_login_page(login_error=str(e)),
            mimetype="text/html", status=429,
        )
    except _auth.AccountDeleted as e:
        # Password was correct, but the account is soft-deleted. Stash the id
        # in the signed session so /account/recover can act on it without the
        # password being re-entered or carried through the page, and show the
        # recovery prompt.
        session.clear()
        session["recover_user_id"] = e.user_id
        return Response(
            _load_login_page(recover_username=username),
            mimetype="text/html", status=200,
        )
    except _auth.AuthError:
        return Response(
            _load_login_page(login_error="Invalid username or password."),
            mimetype="text/html", status=401,
        )
    # Prevent session fixation: drop any prior session contents on login.
    session.clear()
    # If the account predates email 2FA and has no email on file yet, gate the
    # login on collecting and verifying one. We hold the verified user_id in a
    # *separate* session key (pending_email_user_id) so request handlers that
    # check session['user_id'] don't treat them as logged in until the email
    # step completes.
    needs_email = _DO_EMAIL_VERIFICATION and not (user.email or "").strip()
    if needs_email:
        session["pending_email_user_id"] = user.id
        # Carry the legacy-reinit flag through to the add-email step so we
        # can still wipe stale conversations on the way in.
        if was_reinitialized:
            session["pending_email_was_reinitialized"] = True
        return redirect(url_for("add_email_page"))
    session["user_id"] = user.id
    session.permanent = True
    # LEGACY MIGRATION AUTH — verify() auto-upgrades pre-v2 accounts to the
    # current encryption scheme by minting a fresh DEK, which makes any
    # existing conversation files on disk unreadable garbage. Wipe them via
    # the normal delete-all-conversations path. The user database (topics,
    # calendar, preferences) is untouched. Safe to remove once no rows have
    # enc_version < 2.
    if was_reinitialized:
        g.username = user.username
        g.user_id = user.id
        _context_for(user.id).controller.delete_all_sessions()
    # END LEGACY MIGRATION AUTH
    return redirect("/")


# ---------------------------------------------------------------------------
# Add-email-to-existing-account flow (post-login gate for legacy accounts)
# ---------------------------------------------------------------------------


def _finalize_pending_email_login() -> None:
    """Promote the pending_email_* session state into a full login, wiping
    any one-shot flags. Called once the email verification succeeds."""
    uid = session.pop("pending_email_user_id", None)
    was_reinit = session.pop("pending_email_was_reinitialized", False)
    session.pop("pending_email_token", None)
    if uid is None:
        return
    session["user_id"] = uid
    session.permanent = True
    if was_reinit:
        # Same wipe as the standard login path — the freshly minted DEK left
        # the user's stored conversations unreadable.
        user = _auth_backend.lookup(uid)
        if user is not None:
            g.username = user.username
            g.user_id = user.id
            _context_for(uid).controller.delete_all_sessions()


@app.route("/add-email", methods=["GET"])
def add_email_page():
    if not session.get("pending_email_user_id"):
        return redirect(url_for("login_page"))
    return Response(_load_add_email_page(), mimetype="text/html")


@app.route("/add-email/start", methods=["POST"])
def add_email_start():
    uid = session.get("pending_email_user_id")
    if not uid:
        return redirect(url_for("login_page"))
    email = (request.form.get("email") or "").strip()
    try:
        token, code, _norm = _auth_backend.start_add_email_verification(uid, email)
    except _auth.InvalidEmail as e:
        return Response(
            _load_add_email_page(error=str(e)),
            mimetype="text/html", status=400,
        )
    try:
        _email_send.send_verification_code(email, code)
    except _email_send.EmailSendError as e:
        _auth_backend.cancel_verification(token)
        return Response(
            _load_add_email_page(error=str(e)),
            mimetype="text/html", status=502,
        )
    session["pending_email_token"] = token
    return redirect(url_for("add_email_verify_page"))


@app.route("/add-email/verify", methods=["GET"])
def add_email_verify_page():
    uid = session.get("pending_email_user_id")
    token = session.get("pending_email_token")
    if not uid or not token:
        return redirect(url_for("login_page"))
    email = _auth_backend.verification_email(token)
    if email is None:
        session.pop("pending_email_token", None)
        return redirect(url_for("add_email_page"))
    return Response(
        _load_verify_page(
            email=email,
            verify_action=url_for("add_email_verify_submit"),
            resend_action=url_for("add_email_verify_resend"),
            cancel_href=url_for("add_email_verify_cancel"),
            purpose_phrase="finish signing in",
        ),
        mimetype="text/html",
    )


@app.route("/add-email/verify", methods=["POST"])
def add_email_verify_submit():
    uid = session.get("pending_email_user_id")
    token = session.get("pending_email_token")
    if not uid or not token:
        return redirect(url_for("login_page"))
    code = (request.form.get("code") or "").strip()
    try:
        _auth_backend.complete_add_email_verification(token, code)
    except _auth.VerificationError as e:
        email = _auth_backend.verification_email(token)
        if email is None:
            # Attempts exhausted or expired — go back to email entry.
            session.pop("pending_email_token", None)
            return Response(
                _load_add_email_page(
                    error="That code expired or was used up. Please try again.",
                ),
                mimetype="text/html", status=400,
            )
        return Response(
            _load_verify_page(
                email=email,
                verify_action=url_for("add_email_verify_submit"),
                resend_action=url_for("add_email_verify_resend"),
                cancel_href=url_for("add_email_verify_cancel"),
                purpose_phrase="finish signing in",
                error=str(e),
            ),
            mimetype="text/html", status=400,
        )
    _finalize_pending_email_login()
    return redirect("/")


@app.route("/add-email/verify/resend", methods=["POST"])
def add_email_verify_resend():
    if not session.get("pending_email_user_id"):
        return redirect(url_for("login_page"))
    token = session.get("pending_email_token")
    if not token:
        return redirect(url_for("add_email_page"))
    fresh = _auth_backend.resend_verification_code(token)
    if fresh is None:
        session.pop("pending_email_token", None)
        return redirect(url_for("add_email_page"))
    code, email = fresh
    try:
        _email_send.send_verification_code(email, code)
    except _email_send.EmailSendError as e:
        return Response(
            _load_verify_page(
                email=email,
                verify_action=url_for("add_email_verify_submit"),
                resend_action=url_for("add_email_verify_resend"),
                cancel_href=url_for("add_email_verify_cancel"),
                purpose_phrase="finish signing in",
                error=str(e),
            ),
            mimetype="text/html", status=502,
        )
    return Response(
        _load_verify_page(
            email=email,
            verify_action=url_for("add_email_verify_submit"),
            resend_action=url_for("add_email_verify_resend"),
            cancel_href=url_for("add_email_verify_cancel"),
            purpose_phrase="finish signing in",
            notice="We sent a fresh code.",
        ),
        mimetype="text/html",
    )


@app.route("/add-email/verify/cancel", methods=["GET"])
def add_email_verify_cancel():
    token = session.pop("pending_email_token", None)
    session.pop("pending_email_user_id", None)
    session.pop("pending_email_was_reinitialized", None)
    if token:
        _auth_backend.cancel_verification(token)
    return redirect(url_for("login_page"))


@app.route("/signup", methods=["POST"])
def signup_submit():
    # Account creation must be explicitly enabled (AIME_ALLOW_SIGNUP=1).
    # Closed by default so a self-hosted instance can't be joined by strangers.
    if not _ALLOW_SIGNUP:
        return Response(
            _load_login_page(signup_error="Account creation is disabled on this server."),
            mimetype="text/html", status=403,
        )
    # Per-IP throttle so a single host can't bulk-register. Use the direct
    # remote_addr — we don't honor X-Forwarded-For unless explicitly set up
    # to (avoids spoofed-header bypass when running without a trusted proxy).
    if not _signup_limiter.hit(request.remote_addr or "unknown"):
        _auth_backend.log_event(
            _auth.EVENT_SIGNUP_RATE_LIMITED, ip=request.remote_addr,
        )
        return Response(
            _load_login_page(signup_error="Too many signups from this address. Try again later."),
            mimetype="text/html", status=429,
        )
    username = (request.form.get("username") or "").strip()
    email = (request.form.get("email") or "").strip()
    password = request.form.get("password") or ""
    password2 = request.form.get("password2") or ""

    def _signup_err(msg: str, status: int = 400):
        _auth_backend.log_event(
            _auth.EVENT_SIGNUP_FAILED,
            username=username or None, ip=request.remote_addr, detail=msg,
        )
        return Response(
            _load_login_page(
                signup_error=msg,
                signup_username=username,
                signup_email=email,
            ),
            mimetype="text/html", status=status,
        )

    if password != password2:
        return _signup_err("Passwords do not match.")

    # When email verification is off, behave like the pre-2FA signup: create
    # the account directly and log the user in. The Email field is hidden on
    # the form (via _EMAIL_VERIFICATION_DISABLED_STYLE), so any value submitted
    # is ignored.
    if not _DO_EMAIL_VERIFICATION:
        try:
            user, _dek = _auth_backend.create(
                username, password, api_access=(_ACCESS_MODE == "open")
            )
        except _auth.UsernameTaken:
            return _signup_err("That username is already taken.", status=409)
        except _auth.InvalidUsername as e:
            return _signup_err(str(e))
        except _auth.WeakPassword as e:
            return _signup_err(str(e))
        session.clear()
        session["user_id"] = user.id
        session.permanent = True
        return redirect("/")

    try:
        token, code, _email_norm = _auth_backend.start_signup_verification(
            username, password, email,
            api_access=(_ACCESS_MODE == "open"),
        )
    except _auth.UsernameTaken:
        return _signup_err("That username is already taken.", status=409)
    except _auth.InvalidUsername as e:
        return _signup_err(str(e))
    except _auth.WeakPassword as e:
        return _signup_err(str(e))
    except _auth.InvalidEmail as e:
        return _signup_err(str(e))

    try:
        _email_send.send_verification_code(email, code)
    except _email_send.EmailSendError as e:
        # Drop the pending row — we never managed to deliver its code, and
        # leaving it would block a retry on the same username/email.
        _auth_backend.cancel_verification(token)
        return _signup_err(str(e), status=502)

    # Stash only the token in the signed session. Everything else lives in
    # the verifications table and is reached via the token.
    session.clear()
    session["pending_signup_token"] = token
    session.permanent = False
    return redirect(url_for("signup_verify_page"))


@app.route("/signup/verify", methods=["GET"])
def signup_verify_page():
    token = session.get("pending_signup_token")
    if not token:
        return redirect(url_for("login_page"))
    email = _auth_backend.verification_email(token)
    if email is None:
        # Token expired or vanished — clean the session and bounce to login.
        session.pop("pending_signup_token", None)
        return redirect(url_for("login_page"))
    return Response(
        _load_verify_page(
            email=email,
            verify_action=url_for("signup_verify_submit"),
            resend_action=url_for("signup_verify_resend"),
            cancel_href=url_for("signup_verify_cancel"),
            purpose_phrase="finish creating your account",
        ),
        mimetype="text/html",
    )


@app.route("/signup/verify", methods=["POST"])
def signup_verify_submit():
    token = session.get("pending_signup_token")
    if not token:
        return redirect(url_for("login_page"))
    code = (request.form.get("code") or "").strip()
    try:
        user, _dek = _auth_backend.complete_signup_verification(token, code)
    except _auth.VerificationError as e:
        # The backend deletes the row itself once the attempt cap is hit; in
        # that case the token still in the session is now meaningless.
        email = _auth_backend.verification_email(token)
        if email is None:
            session.pop("pending_signup_token", None)
            return Response(
                _load_login_page(
                    signup_error="That verification expired — please sign up again.",
                ),
                mimetype="text/html", status=400,
            )
        return Response(
            _load_verify_page(
                email=email,
                verify_action=url_for("signup_verify_submit"),
                resend_action=url_for("signup_verify_resend"),
                cancel_href=url_for("signup_verify_cancel"),
                purpose_phrase="finish creating your account",
                error=str(e),
            ),
            mimetype="text/html", status=400,
        )
    except _auth.UsernameTaken:
        # A different signup completed for this username while the code was
        # outstanding — extremely unlikely, but recoverable: bounce back to
        # signup so they can pick another name.
        session.pop("pending_signup_token", None)
        return Response(
            _load_login_page(
                signup_error="That username was just taken — please pick another.",
            ),
            mimetype="text/html", status=409,
        )

    # Promote: clear the pending state, log the new account in.
    session.clear()
    session["user_id"] = user.id
    session.permanent = True
    return redirect("/")


@app.route("/signup/verify/resend", methods=["POST"])
def signup_verify_resend():
    token = session.get("pending_signup_token")
    if not token:
        return redirect(url_for("login_page"))
    fresh = _auth_backend.resend_verification_code(token)
    if fresh is None:
        session.pop("pending_signup_token", None)
        return redirect(url_for("login_page"))
    code, email = fresh
    try:
        _email_send.send_verification_code(email, code)
    except _email_send.EmailSendError as e:
        return Response(
            _load_verify_page(
                email=email,
                verify_action=url_for("signup_verify_submit"),
                resend_action=url_for("signup_verify_resend"),
                cancel_href=url_for("signup_verify_cancel"),
                purpose_phrase="finish creating your account",
                error=str(e),
            ),
            mimetype="text/html", status=502,
        )
    return Response(
        _load_verify_page(
            email=email,
            verify_action=url_for("signup_verify_submit"),
            resend_action=url_for("signup_verify_resend"),
            cancel_href=url_for("signup_verify_cancel"),
            purpose_phrase="finish creating your account",
            notice="We sent a fresh code.",
        ),
        mimetype="text/html",
    )


@app.route("/signup/verify/cancel", methods=["GET"])
def signup_verify_cancel():
    token = session.pop("pending_signup_token", None)
    if token:
        _auth_backend.cancel_verification(token)
    return redirect(url_for("login_page"))


@app.route("/logout", methods=["POST"])
def logout():
    # POST-only so a stray <img src="/logout"> or prefetched link can't
    # silently log the user out. The frontend already POSTs via fetch().
    session.clear()
    return redirect(url_for("login_page"))


@app.route("/account/recover", methods=["POST"])
def account_recover():
    """Restore a soft-deleted account the user just tried to log into.

    Reached only from the recovery prompt: login_submit() puts the verified
    user id into the signed session as `recover_user_id`. Restoring just
    clears the soft-delete flag — we still bounce back to the login page
    rather than auto-logging-in, so the user goes through the normal
    password-verify path and any access-control re-stamping (api_access)
    happens in one place.
    """
    uid = session.get("recover_user_id")
    session.pop("recover_user_id", None)
    if not uid:
        # No handoff in the session — nothing to recover. Back to login.
        return redirect(url_for("login_page"))
    # Re-stamp api_access using the same logic as signup: open mode grants
    # immediately, keys mode forces a fresh key redemption. Prevents a
    # soft-deleted account from skipping the gate on its way back.
    _auth_backend.restore(uid, api_access=(_ACCESS_MODE == "open"))
    user = _auth_backend.lookup(uid)
    return Response(
        _load_login_page(
            notice="Your account has been recovered. Please sign in.",
            login_username=user.username if user else "",
        ),
        mimetype="text/html",
    )


@app.route("/me")
@login_required
def me():
    # access_mode + api_access let the frontend show the invite-key field in
    # profile settings and disable the composer when sending is gated.
    user = _auth_backend.lookup(g.user_id)
    return jsonify({
        "id": g.user_id,
        "username": g.username,
        "email": user.email if user else None,
        "access_mode": _ACCESS_MODE,
        "api_access": g.api_access,
    })


@app.route("/redeem", methods=["POST"])
@login_required
def redeem():
    """Redeem an invite key for the logged-in user. On success the user's
    api_access flag is set and message sending unlocks. Available in any
    access mode (redeeming in "open" mode just pre-grants access)."""
    data = request.get_json(silent=True) or {}
    key = (data.get("key") or "").strip()
    if not key:
        return jsonify({"ok": False, "error": "empty",
                        "message": "Enter an invite key."}), 400
    if _auth_backend.redeem_key(g.user_id, key):
        return jsonify({"ok": True})
    return jsonify({"ok": False, "error": "invalid",
                    "message": "That key is invalid or has already been used."}), 400


# ---------------------------------------------------------------------------
# Account data export / import
#
# A user can download their whole data set as an unencrypted zip bundle and
# upload one back (e.g. migrating from a local install). The bundle layout:
#   aime-export.json        manifest (format tag, version, username, date)
#   database.sql            calendar + topic metadata
#   topics/<file>.md        topic content files
#   conversations/<id>.json decrypted conversation transcripts
# Import re-encrypts conversations under the importing account's key.
# ---------------------------------------------------------------------------

_BUNDLE_MANIFEST = "aime-export.json"
_BUNDLE_FORMAT = "aime-data-export"


# Directory components and filenames that operating systems and archive
# tools scatter into zips. They carry no Aime data, are often invisible in
# the user's file manager, and must never break or be rejected by an import.
_BUNDLE_JUNK_DIRS = {
    "__MACOSX", ".Spotlight-V100", ".Trashes", ".fseventsd",
    ".TemporaryItems", ".DocumentRevisions-V100", "$RECYCLE.BIN",
    "System Volume Information",
}
_BUNDLE_JUNK_FILES = {".DS_Store", "Thumbs.db", "desktop.ini"}


def _is_bundle_junk(name: str) -> bool:
    """True for OS / archiver cruft that should be silently ignored — macOS
    __MACOSX entries and AppleDouble ._ sidecars, .DS_Store, Windows
    Thumbs.db, and friends."""
    parts = [p for p in name.split("/") if p]
    if not parts:
        return True
    if any(p in _BUNDLE_JUNK_DIRS for p in parts):
        return True
    leaf = parts[-1]
    if leaf in _BUNDLE_JUNK_FILES:
        return True
    if leaf.startswith("._"):  # AppleDouble resource-fork sidecar
        return True
    return False


def _bundle_path_is_safe(name: str) -> bool:
    """Reject path traversal and absolute paths before any file is written."""
    if name.startswith("/") or "\\" in name:
        return False
    return ".." not in name.split("/")


def _classify_bundle(zf: zipfile.ZipFile) -> tuple[dict | None, str | None]:
    """Inspect an uploaded zip and locate the Aime data inside it.

    Tolerates a wrapping folder (a zip of a directory rather than its
    contents) and OS junk: database.sql is found wherever it sits, and its
    location fixes the bundle root. topics/ and conversations/ are both
    optional. Unknown extra files are ignored rather than rejected.

    Returns (plan, None) on success or (None, error_message) on failure.
    plan = {"db": <zip name>,
            "topics": [(zip name, leaf), ...],
            "conversations": [(zip name, session_id), ...],
            "skipped": <int>}
    """
    entries: list[str] = []
    for info in zf.infolist():
        name = info.filename
        if name.endswith("/"):
            continue  # directory entry
        if _is_bundle_junk(name):
            continue
        if not _bundle_path_is_safe(name):
            return None, f"Unsafe path in bundle: {name}"
        entries.append(name)

    db_candidates = [n for n in entries if n.rsplit("/", 1)[-1] == "database.sql"]
    if not db_candidates:
        return None, ("Bundle has no database.sql — it does not look like "
                       "Aime data.")
    if len(db_candidates) > 1:
        return None, ("Bundle contains multiple database.sql files — unzip it "
                       "and upload just one account's data.")
    db_name = db_candidates[0]
    root = db_name[: -len("database.sql")]  # "" (flat) or "MyBackup/"

    topics: list[tuple[str, str]] = []
    conversations: list[tuple[str, str]] = []
    skipped = 0
    for name in entries:
        if name == db_name:
            continue
        if not name.startswith(root):
            skipped += 1  # sits outside the bundle root — unrelated file
            continue
        parts = name[len(root):].split("/")
        if len(parts) == 2 and parts[0] == "topics" and parts[1].endswith(".md"):
            topics.append((name, parts[1]))
        elif (len(parts) == 2 and parts[0] == "conversations"
              and parts[1].endswith(".json")):
            conversations.append((name, parts[1][: -len(".json")]))
        elif parts[-1] != _BUNDLE_MANIFEST:
            skipped += 1  # unknown extra file — ignore, don't fail

    return {"db": db_name, "topics": topics,
            "conversations": conversations, "skipped": skipped}, None


def _evict_user_context(user_id: int) -> None:
    """Drop the cached UserContext so the next request rebuilds it from disk.
    Used after an import replaces the user's files out from under it."""
    with _user_contexts_lock:
        ctx = _user_contexts.pop(user_id, None)
    if ctx is not None:
        try:
            ctx.controller.shutdown()
        except Exception as e:  # noqa: BLE001 - teardown must not fail import
            print(f"[import] controller shutdown failed: {e}", file=sys.stderr)


def _reload_backend_database(user_id: int) -> None:
    """Ask the C++ backend to drop its cached sqlite handle for this user so
    it re-opens database.sql after an import. Best-effort — a backend restart
    would achieve the same, so a failure here is logged, not fatal."""
    try:
        requests.post(
            aime_config.API_URL,
            json={"tool_name": "reload_database", "user_id": user_id},
            timeout=5,
        )
    except requests.RequestException as e:
        print(f"[import] reload_database call failed: {e}", file=sys.stderr)


@app.route("/account/export")
@login_required
def account_export():
    """Stream the logged-in user's data as an unencrypted zip bundle."""
    user_id = g.user_id
    try:
        dek = _auth_backend.get_dek(user_id)
    except _auth.BackgroundUnavailable:
        # Pre-v2 account that hasn't been logged in since the encryption
        # upgrade. login_required got us this far only because the cookie
        # was valid; force re-login so verify() can auto-upgrade.
        session.clear()
        return jsonify({"ok": False, "error": "auth required"}), 401

    user_dir = _user_dir(user_id)
    buf = BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(_BUNDLE_MANIFEST, json.dumps({
            "format": _BUNDLE_FORMAT,
            "version": 1,
            "user_id": user_id,
            "username": g.username,
            "exported_at": datetime.datetime.now().isoformat(timespec="seconds"),
        }, indent=2))

        db_path = os.path.join(user_dir, "database.sql")
        if os.path.exists(db_path):
            # Consistent snapshot — the backend may hold the file open.
            with tempfile.NamedTemporaryFile(delete=False) as tmp:
                tmp_path = tmp.name
            try:
                _backup.snapshot_sqlite(db_path, tmp_path)
                zf.write(tmp_path, "database.sql")
            finally:
                os.unlink(tmp_path)

        topics_dir = os.path.join(user_dir, "topics")
        if os.path.isdir(topics_dir):
            for name in sorted(os.listdir(topics_dir)):
                path = os.path.join(topics_dir, name)
                if os.path.isfile(path):
                    zf.write(path, f"topics/{name}")

        conv_dir = _conversations_dir(user_id)
        if os.path.isdir(conv_dir):
            for name in sorted(os.listdir(conv_dir)):
                if not name.endswith(".json.enc"):
                    continue
                session_id = name[: -len(".json.enc")]
                try:
                    with open(os.path.join(conv_dir, name), "rb") as f:
                        blob = f.read()
                    plaintext = _enc.decrypt_blob(
                        dek, blob, aad=session_id.encode("utf-8")
                    )
                except Exception as e:  # noqa: BLE001 - skip a corrupt file
                    print(f"[export] skipped {name}: {e}", file=sys.stderr)
                    continue
                zf.writestr(f"conversations/{session_id}.json", plaintext)

    stamp = datetime.datetime.now().strftime("%Y%m%d")
    fname = f"aime-export-{g.username}-{stamp}.zip"
    return Response(
        buf.getvalue(),
        mimetype="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


@app.route("/account/import", methods=["POST"])
@login_required
def account_import():
    """Replace the logged-in user's data with an uploaded bundle. The current
    data is backed up first; conversations are re-encrypted under this
    account's key."""
    user_id = g.user_id
    try:
        dek = _auth_backend.get_dek(user_id)
    except _auth.BackgroundUnavailable:
        session.clear()
        return jsonify({"ok": False, "error": "auth required"}), 401

    upload = request.files.get("bundle")
    if upload is None:
        return jsonify({"ok": False, "error": "no_file",
                        "message": "No bundle file was uploaded."}), 400
    try:
        zf = zipfile.ZipFile(BytesIO(upload.read()))
    except zipfile.BadZipFile:
        return jsonify({"ok": False, "error": "bad_zip",
                        "message": "That file is not a valid zip bundle."}), 400

    with zf:
        # Validate the bundle by its contents, never by a manifest — a
        # hand-assembled bundle (e.g. zipping a local install's data directory
        # straight up) has no manifest, and a manifest could be forged anyway.
        # _classify_bundle tolerates a wrapping folder and ignores OS junk
        # (macOS __MACOSX/._ sidecars, .DS_Store, Windows Thumbs.db, …) so a
        # bundle the user can't easily clean up still imports cleanly.
        plan, err = _classify_bundle(zf)
        if plan is None:
            return jsonify({"ok": False, "error": "bad_bundle",
                            "message": err}), 400

        # Safety net: snapshot the current data before replacing it.
        backup_path = _backup.backup_user_data(user_id, reason="import")

        # Detach the in-memory session so nothing writes during the swap.
        _evict_user_context(user_id)

        user_dir = _user_dir(user_id)
        os.makedirs(user_dir, exist_ok=True)
        db_dst = os.path.join(user_dir, "database.sql")
        topics_dst = os.path.join(user_dir, "topics")
        conv_dst = os.path.join(user_dir, "conversations")
        if os.path.exists(db_dst):
            os.remove(db_dst)
        shutil.rmtree(topics_dst, ignore_errors=True)
        shutil.rmtree(conv_dst, ignore_errors=True)
        os.makedirs(topics_dst, exist_ok=True)
        os.makedirs(conv_dst, exist_ok=True)

        # Write everything into the current standard layout, regardless of
        # how the uploaded bundle was structured.
        with open(db_dst, "wb") as f:
            f.write(zf.read(plan["db"]))
        for name, leaf in plan["topics"]:
            with open(os.path.join(topics_dst, leaf), "wb") as f:
                f.write(zf.read(name))
        for name, session_id in plan["conversations"]:
            blob = _enc.encrypt_blob(
                dek, zf.read(name), aad=session_id.encode("utf-8")
            )
            with open(os.path.join(conv_dst, session_id + ".json.enc"), "wb") as f:
                f.write(blob)

    # Make the C++ backend re-open the freshly written database.sql.
    _reload_backend_database(user_id)

    summary = (f"Imported {len(plan['topics'])} topic(s) and "
               f"{len(plan['conversations'])} conversation(s).")
    if plan["skipped"]:
        summary += f" {plan['skipped']} unrecognized file(s) were ignored."

    return jsonify({
        "ok": True,
        "backup": os.path.basename(backup_path) if backup_path else None,
        "message": summary + " The page will now reload.",
    })


@app.route("/account/delete", methods=["POST"])
@login_required
def account_delete():
    """Soft-delete the logged-in user's own account.

    This is a *reversible* deactivation: the account row is flagged (see
    aime.auth.soft_delete) but the data directory is left fully intact, so the
    user can recover it by signing in again during the grace period. The
    permanent purge is a separate admin step (scripts/manage_users.py purge).
    """
    uid = g.user_id
    _auth_backend.soft_delete(uid)
    # Tear down the in-memory session — the account is now disabled and the
    # next request must not resolve to it.
    _evict_user_context(uid)
    session.clear()
    return jsonify({"ok": True})


@app.route("/")
@login_required
def index():
    return Response(_load_page(), mimetype="text/html")


_ALLOWED_IMAGE_TYPES = {"image/png", "image/jpeg", "image/gif", "image/webp"}

# The model API rejects images larger than 5 MB; aim under that so the base64
# overhead the attachment picks up in transit can't push it back over. We use
# the same number as a soft cap for non-image uploads so files and images
# behave symmetrically from the user's perspective.
_IMAGE_SIZE_TARGET = int(4.5 * 1024 * 1024)
_MAX_UPLOAD_BYTES = _IMAGE_SIZE_TARGET
# Upper bound on how much text we inline from an arbitrary uploaded file, so a
# huge log or binary blob can't blow up the conversation context.
_MAX_TEXT_CHARS = 200_000


def _convert_image(raw: bytes) -> dict | None:
    """Decode `raw` with Pillow and re-encode it as PNG or JPEG, downscaling
    until it fits `_IMAGE_SIZE_TARGET`. Returns a {"media_type", "data"} dict
    with base64 data, or None if the bytes are not a decodable image."""
    if not _PIL_AVAILABLE:
        return None
    try:
        im = Image.open(BytesIO(raw))
        im.load()
    except Exception:  # noqa: BLE001 - not an image (or an unsupported one)
        return None
    # An animated GIF survives re-encoding only as a single flattened frame,
    # so pass the original through untouched when it already fits.
    if (im.format or "").upper() == "GIF" and getattr(im, "is_animated", False) \
            and len(raw) <= _IMAGE_SIZE_TARGET:
        return {"media_type": "image/gif",
                "data": base64.b64encode(raw).decode("ascii")}
    # Honour any EXIF orientation, then flatten to a mode the encoders accept.
    im = ImageOps.exif_transpose(im) or im
    has_alpha = im.mode in ("RGBA", "LA") or (
        im.mode == "P" and "transparency" in im.info)
    if has_alpha:
        im = im.convert("RGBA")
        out_fmt, media_type = "PNG", "image/png"
    else:
        im = im.convert("RGB")
        out_fmt, media_type = "JPEG", "image/jpeg"
    quality = 90
    scale = 1.0
    best = b""
    # Alternate between lowering JPEG quality and shrinking dimensions until
    # the encoded result fits, capped so a pathological image can't loop on.
    for _ in range(16):
        if scale != 1.0:
            w = max(1, int(im.width * scale))
            h = max(1, int(im.height * scale))
            frame = im.resize((w, h))
        else:
            frame = im
        buf = BytesIO()
        if out_fmt == "JPEG":
            frame.save(buf, "JPEG", quality=quality)
        else:
            frame.save(buf, "PNG", optimize=True)
        best = buf.getvalue()
        if len(best) <= _IMAGE_SIZE_TARGET:
            break
        if out_fmt == "JPEG" and quality > 50:
            quality -= 15
        else:
            scale *= 0.8
    return {"media_type": media_type,
            "data": base64.b64encode(best).decode("ascii")}


@app.route("/upload", methods=["POST"])
@login_required
@api_access_required
def upload():
    """Normalise an arbitrary uploaded file into an attachment the model can
    consume. Images of any format are decoded and re-encoded as PNG/JPEG via
    Pillow; everything else is read as text so that, worst case, the user
    still gets *something* through rather than a hard rejection."""
    f = request.files.get("file")
    if f is None:
        return jsonify({"ok": False, "error": "no_file",
                        "message": "No file was uploaded."}), 400
    name = os.path.basename(f.filename or "") or "file"
    raw = f.read()
    if not raw:
        return jsonify({"ok": False, "error": "empty_file",
                        "message": "That file is empty."}), 400
    # Files and images share the same soft cap. We still attach what we can —
    # `_convert_image` will downsample images to fit, and the text path
    # truncates well before this — but flag it so the client can show a
    # friendly notice that part of the file may not make it through.
    oversized = len(raw) > _MAX_UPLOAD_BYTES

    img = _convert_image(raw)
    if img is not None:
        return jsonify({"ok": True, "kind": "image", "name": name,
                        "media_type": img["media_type"], "data": img["data"],
                        "oversized": oversized})

    # Not a decodable image — fall back to text. Decode tolerantly so even a
    # binary file yields readable content instead of failing the upload.
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        text = raw.decode("utf-8", errors="replace")
    truncated = len(text) > _MAX_TEXT_CHARS
    if truncated:
        text = text[:_MAX_TEXT_CHARS] + "\n…[truncated]"
    return jsonify({"ok": True, "kind": "file", "name": name,
                    "text": text, "truncated": truncated,
                    "oversized": oversized})


@app.route("/send", methods=["POST"])
@login_required
@api_access_required
def send():
    data = request.get_json(silent=True) or {}
    text = (data.get("text") or "").strip()
    raw_images = data.get("images") or []
    images: list[dict] = []
    for img in raw_images:
        if not isinstance(img, dict):
            continue
        media_type = img.get("media_type")
        b64 = img.get("data")
        if media_type in _ALLOWED_IMAGE_TYPES and isinstance(b64, str) and b64:
            images.append({"media_type": media_type, "data": b64})
    if not text and not images:
        return jsonify({"ok": False, "error": "empty"}), 400
    ctx = _context_for(g.user_id)
    # The browser sends its IANA timezone (e.g. "America/New_York") with each
    # message so per-turn timestamps the model sees track the user's local
    # time. Refreshed every send — self-corrects if the user travels.
    tz = data.get("tz")
    if isinstance(tz, str) and tz:
        ctx.controller.set_client_timezone(tz)
    stale_tag = ctx.drain_stale_tag()
    should_quit = ctx.controller.dispatch_input(
        text, images=images or None, hidden_prefix=stale_tag,
    )
    return jsonify({"ok": True, "quit": should_quit})


@app.route("/interrupt", methods=["POST"])
@login_required
def interrupt():
    """Stop the in-flight assistant turn and block until it has actually
    ended. Clients can safely POST /send as soon as this returns — the
    controller is guaranteed to be idle. Returns 503 if the turn does not
    end within the timeout (rare; usually means the model stream is stuck
    waiting on the network)."""
    ctx = _context_for(g.user_id)
    became_idle = ctx.controller.stop_model(timeout=5.0)
    if not became_idle:
        return jsonify({"ok": False, "error": "interrupt timed out"}), 503
    return jsonify({"ok": True})


@app.route("/stream")
@login_required
def stream():
    ctx = _context_for(g.user_id)
    q, snapshot = ctx.attach_client()

    def gen():
        try:
            for payload in snapshot:
                yield f"data: {json.dumps(payload)}\n\n"
            # Sentinel: from here on, events are live (typewriter eligible).
            # `busy` carries the real turn state so a client that just
            # replayed history (where turn_end/ready don't appear) knows
            # whether the model is mid-response.
            yield (
                "data: "
                + json.dumps({
                    "kind": "history_done",
                    "busy": not ctx.controller.is_idle,
                })
                + "\n\n"
            )
            while True:
                payload = q.get()
                yield f"data: {json.dumps(payload)}\n\n"
        finally:
            ctx.detach_client(q)

    return Response(gen(), mimetype="text/event-stream")


@app.route("/sessions")
@login_required
def sessions():
    items = [
        {"id": s.id, "summary": s.summary, "saved_at": s.saved_at}
        for s in _context_for(g.user_id).controller.list_sessions()
    ]
    return jsonify({"sessions": items})


@app.route("/sessions/<session_id>", methods=["DELETE"])
@login_required
def delete_session(session_id: str):
    _context_for(g.user_id).controller.delete_session(session_id)
    return jsonify({"ok": True})


@app.route("/sessions", methods=["DELETE"])
@login_required
def delete_all_sessions():
    _context_for(g.user_id).controller.delete_all_sessions()
    return jsonify({"ok": True})


@app.route("/calendar/<int:year>/<int:month>")
@login_required
def calendar_month(year: int, month: int):
    try:
        events = _context_for(g.user_id).calendar_service.events_for_month(year, month, include_archived=False)
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500
    return jsonify({"events": events})


@app.route("/calendar/<int:year>/<int:month>/<int:day>")
@login_required
def calendar_day(year: int, month: int, day: int):
    try:
        events = sort_events_by_date(
            _context_for(g.user_id).calendar_service.events_for_day(year, month, day, include_archived=False)
        )
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500
    return jsonify({"events": events})


@app.route("/calendar/events/<int:event_id>", methods=["PUT"])
@login_required
def calendar_event_update(event_id: int):
    """Replace an event's fields (and/or toggle archived). The frontend
    sends the full record back, which matches the backend tool's contract."""
    data = request.get_json(silent=True) or {}
    title = data.get("title")
    summary = data.get("summary")
    category = data.get("category")
    date = data.get("date")
    time_ = data.get("time", "")
    archived = data.get("archived", False)
    if not isinstance(title, str) or not isinstance(date, str):
        return jsonify({"ok": False, "error": "title and date are required"}), 400
    # Lifecycle metadata is optional from the UI: only the fields actually sent
    # are forwarded, so anything omitted is preserved by the backend's merge
    # (e.g. saving a description edit never resets status or commitment_id).
    extra = {
        key: data[key]
        for key in ("status", "commitment_id", "status_change_reason", "rescheduled_from")
        if isinstance(data.get(key), str)
    }
    ctx = _context_for(g.user_id)
    try:
        result = ctx.calendar_service.replace_event(
            event_id,
            title=title,
            summary=summary if isinstance(summary, str) else "",
            category=category if isinstance(category, str) else "",
            date=date,
            time=time_ if isinstance(time_, str) else "",
            archived=bool(archived),
            **extra,
        )
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500
    ctx.mark_record_stale("event", event_id, title)
    return jsonify({"ok": True, "result": result})


@app.route("/stt/models", methods=["GET"])
@login_required
def stt_models():
    return jsonify(_stt.list_models())


@app.route("/stt/models", methods=["POST"])
@login_required
def stt_models_set():
    data = request.get_json(silent=True) or {}
    name = data.get("name")
    if not isinstance(name, str):
        return jsonify({"ok": False, "error": "name required"}), 400
    try:
        path = _stt.set_selected_model(name)
    except _stt.STTError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    return jsonify({"ok": True, "name": name, "path": path})


@app.route("/transcribe", methods=["POST"])
@login_required
def transcribe():
    """Accepts a WAV blob (mono 16-bit PCM) and returns recognized text.

    The client may POST either raw `audio/wav` bytes or a multipart form with
    an `audio` file field. Returns {"ok": true, "text": "..."} on success.
    """
    wav_bytes: bytes
    if request.files and "audio" in request.files:
        wav_bytes = request.files["audio"].read()
    else:
        wav_bytes = request.get_data() or b""
    if not wav_bytes:
        return jsonify({"ok": False, "error": "empty audio"}), 400
    try:
        text = _stt.transcribe_wav(wav_bytes, user=g.get("username"))
    except _stt.STTError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    except Exception as exc:  # noqa: BLE001
        return jsonify({"ok": False, "error": f"stt failed: {exc}"}), 500
    return jsonify({"ok": True, "text": text})


@app.route("/topics")
@login_required
def topics():
    try:
        items = _context_for(g.user_id).topic_service.list_topics()
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500
    return jsonify({"topics": items})


@app.route("/topics/<topic_id>")
@login_required
def topic_contents(topic_id: str):
    if not topic_id.isdigit():
        return jsonify({"ok": False, "error": "invalid topic id"}), 400
    try:
        contents = _context_for(g.user_id).topic_service.get_topic_contents(int(topic_id))
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500
    return jsonify({"contents": contents})


_EXPORT_FORMATS = {
    # ui-name -> (pandoc target, file extension, mime type)
    "md":   (None,    "md",   "text/markdown"),
    "html": ("html5", "html", "text/html"),
    "docx": ("docx",  "docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
    "odt":  ("odt",   "odt",  "application/vnd.oasis.opendocument.text"),
    "pdf":  ("pdf",   "pdf",  "application/pdf"),
    "epub": ("epub",  "epub", "application/epub+zip"),
    "rtf":  ("rtf",   "rtf",  "application/rtf"),
    "txt":  ("plain", "txt",  "text/plain"),
}


def _safe_filename(name: str, fallback: str) -> str:
    # Strip path separators and control chars; keep it readable across OSes.
    cleaned = re.sub(r"[^\w\-. ]+", "", (name or "").strip())
    cleaned = cleaned.strip(" .")[:80]
    return cleaned or fallback


@app.route("/topics/<topic_id>/export")
@login_required
def topic_export(topic_id: str):
    if not topic_id.isdigit():
        return jsonify({"ok": False, "error": "invalid topic id"}), 400
    fmt = (request.args.get("format") or "md").lower()
    if fmt not in _EXPORT_FORMATS:
        return jsonify({"ok": False, "error": "unsupported format"}), 400
    target, ext, mime = _EXPORT_FORMATS[fmt]
    ctx = _context_for(g.user_id)
    try:
        markdown = ctx.topic_service.get_topic_contents(int(topic_id))
        # Title comes from the topics list — get_topic_contents only returns
        # the body, not metadata. A small list scan is fine (topics are tens,
        # not thousands) and keeps this route independent of how the gateway
        # exposes single-topic metadata.
        title = ""
        for t in ctx.topic_service.list_topics():
            if str(t.get("id") or t.get("topic_id") or "") == topic_id:
                title = t.get("title") or t.get("name") or ""
                break
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500

    filename = f"{_safe_filename(title, f'topic-{topic_id}')}.{ext}"
    if target is None:
        # Raw markdown — no pandoc needed.
        data = markdown.encode("utf-8")
    else:
        if not _PANDOC_AVAILABLE:
            return jsonify({
                "ok": False,
                "error": "export to this format is unavailable on the server",
            }), 503
        try:
            if target == "pdf":
                # Skip pandoc's --pdf-engine machinery (which depends on
                # external binaries on PATH and has finicky engine/format
                # compatibility rules). Render to HTML with pandoc, then
                # rasterize with WeasyPrint as an in-process Python call.
                if not _WEASY_AVAILABLE:
                    raise RuntimeError(
                        "PDF export needs WeasyPrint, which isn't installed "
                        "on the server"
                    )
                html = _pypandoc.convert_text(
                    markdown, "html5", format="md",
                    extra_args=["--standalone"],
                )
                data = _WeasyHTML(string=html).write_pdf()
            elif target in ("docx", "odt", "epub"):
                with tempfile.NamedTemporaryFile(
                    suffix=f".{ext}", delete=False
                ) as tf:
                    tmp_path = tf.name
                try:
                    _pypandoc.convert_text(
                        markdown, target, format="md", outputfile=tmp_path,
                    )
                    with open(tmp_path, "rb") as f:
                        data = f.read()
                finally:
                    try:
                        os.unlink(tmp_path)
                    except OSError:
                        pass
            else:
                text = _pypandoc.convert_text(markdown, target, format="md")
                data = text.encode("utf-8")
        except OSError as exc:
            # Missing PDF engine is the common one — surface a clean message
            # instead of a stack trace.
            return jsonify({
                "ok": False,
                "error": f"conversion failed: {exc}",
            }), 500
        except Exception as exc:  # noqa: BLE001
            return jsonify({
                "ok": False,
                "error": f"conversion failed: {exc}",
            }), 500

    return Response(
        data,
        mimetype=mime,
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": "no-store",
        },
    )


@app.route("/topics/<topic_id>", methods=["PUT"])
@login_required
def topic_contents_save(topic_id: str):
    if not topic_id.isdigit():
        return jsonify({"ok": False, "error": "invalid topic id"}), 400
    data = request.get_json(silent=True) or {}
    contents = data.get("contents")
    if not isinstance(contents, str):
        return jsonify({"ok": False, "error": "contents (string) required"}), 400
    # Hard cap on topic body size — prevents a malicious or runaway client
    # from filling the disk via repeated PUTs.
    if len(contents.encode("utf-8")) > 2 * 1024 * 1024:
        return jsonify({"ok": False, "error": "contents too large (max 2 MiB)"}), 413
    ctx = _context_for(g.user_id)
    tid = int(topic_id)
    try:
        ctx.topic_service.replace_topic_contents(tid, contents)
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500
    # Look up the title so the <stale> tag carries a recognizable name. If the
    # list call fails for any reason, fall through with an empty title — the id
    # alone is still enough for the model to know the record changed.
    title = ""
    try:
        for t in ctx.topic_service.list_topics():
            if int(t.get("id", -1)) == tid:
                title = (t.get("title") or "").strip()
                break
    except Exception:
        pass
    ctx.mark_record_stale("topic", tid, title)
    return jsonify({"ok": True})


def _load_or_create_tls_context():
    """Return a (certfile, keyfile) pair for HTTPS, generating a persistent
    self-signed cert in DATABASE_DIR on first use.

    Browsers only expose getUserMedia (mic access) in a secure context, so a
    phone reaching the app over the LAN IP needs TLS — plain http there has
    no microphone at all. The cert is self-signed, so the browser shows a
    one-time "not trusted" warning; because it persists across restarts you
    only have to accept it once per device."""
    cert_path = os.path.join(aime_config.DATABASE_DIR, "tls_cert.pem")
    key_path = os.path.join(aime_config.DATABASE_DIR, "tls_key.pem")
    if os.path.exists(cert_path) and os.path.exists(key_path):
        return (cert_path, key_path)

    from datetime import datetime, timedelta, timezone
    from cryptography import x509
    from cryptography.x509.oid import NameOID
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Aime")])
    now = datetime.now(timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=3650))
        .add_extension(
            x509.SubjectAlternativeName([x509.DNSName("localhost")]),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    with open(key_path, "wb") as fh:
        fh.write(key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        ))
    os.chmod(key_path, 0o600)
    with open(cert_path, "wb") as fh:
        fh.write(cert.public_bytes(serialization.Encoding.PEM))
    return (cert_path, key_path)


if __name__ == "__main__":
    # threaded=True so the SSE generator and /send can run concurrently.
    # Bind defaults to 127.0.0.1 (loopback only). Set AIME_BIND=0.0.0.0 to
    # expose the web UI to other devices on the LAN — useful for testing from
    # a second machine, NOT for production. Anyone on the network will be
    # able to hit /login, and unless AIME_HTTPS=1 + a TLS terminator is in
    # front, the session cookie travels in cleartext.
    host = os.environ.get("AIME_BIND", "127.0.0.1")
    port = int(os.environ.get("AIME_PORT", "5000"))
    # AIME_HTTPS=1 serves over TLS with a persistent self-signed cert. Needed
    # for microphone/voice input from phones on the LAN (secure-context rule).
    ssl_context = None
    if int(os.environ.get("AIME_HTTPS", "0")):
        ssl_context = _load_or_create_tls_context()
    app.run(host=host, port=port, threaded=True, debug=False,
            ssl_context=ssl_context)
