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
import threading
from functools import wraps
from io import StringIO

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

from . import stt as _stt


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


# In-process cache of unwrapped DEKs, keyed by user_id. Populated by
# login/signup, dropped on logout. Lives only in process memory by design:
# a server restart forces re-login, which is the correct property — without
# the password we can't re-derive the KEK that unwraps the DEK.
_dek_cache: dict[int, bytes] = {}
_dek_cache_lock = threading.Lock()


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


# LEGACY MIGRATION — encrypt any plaintext *.json conversation files in a
# user's directory, rewriting them as *.json.enc under the user's DEK and
# removing the originals. Runs once per UserContext construction (i.e. once
# per login per process). Idempotent: pure-encrypted directories are a no-op.
# Safe to remove once all live installs have been upgraded past this version.
def _encrypt_plaintext_conversations(conv_dir: str, dek: bytes) -> None:
    if not os.path.isdir(conv_dir):
        return
    try:
        names = os.listdir(conv_dir)
    except OSError:
        return
    converted = 0
    for name in names:
        if not name.endswith(".json") or name.endswith(".json.enc"):
            continue
        session_id = name[: -len(".json")]
        src = os.path.join(conv_dir, name)
        dst = os.path.join(conv_dir, session_id + ".json.enc")
        if os.path.exists(dst):
            # Encrypted version already present — drop the plaintext.
            try:
                os.remove(src)
            except OSError:
                pass
            continue
        try:
            with open(src, "rb") as f:
                plaintext = f.read()
            blob = _enc.encrypt_blob(dek, plaintext, aad=session_id.encode("utf-8"))
            tmp = dst + ".tmp"
            with open(tmp, "wb") as f:
                f.write(blob)
            os.replace(tmp, dst)
            os.remove(src)
            converted += 1
        except OSError:
            continue
    if converted:
        print(
            f"[migration] encrypted {converted} plaintext conversation(s) in {conv_dir}",
            file=sys.stderr,
        )
# END LEGACY MIGRATION


# ---------------------------------------------------------------------------
# Per-user controller / SSE state
# ---------------------------------------------------------------------------

_HR_LINE_RE = re.compile(r"(?m)^[ \t]*---[ \t]*$")
# A unicode sentinel the model is overwhelmingly unlikely to produce on its own,
# used to mark horizontal-rule positions through Rich's renderer so we can swap
# them for <hr> in the final HTML.
_HR_SENTINEL = "❦AIMEHR❦"


def _safe_markup_text(markup: str) -> Text:
    """Forgiving version of `Text.from_markup` — render the markup the model
    *got right* even when part of it is malformed.

    `Text.from_markup` is all-or-nothing: a single stray closing tag, a
    mismatched close, or an unknown style name raises and the whole message
    drops to plain text. That is jarring mid-conversation — the formatting was
    visibly fine while streaming, then a small slip at the end wipes it out.

    Streaming already looks correct because a half-typed message only has
    *unclosed* tags, which Rich tolerates (it closes them implicitly at the
    end). This applies the same forgiveness to every render: stray/unmatched
    closing tags are dropped, unknown style names are shown literally as their
    `[tag]` text, and anything still open at the end is closed implicitly.
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
        if span_style:
            text.spans.append(Span(start, end, span_style))
    text.spans.sort(key=lambda span: span.start)
    return text


def _render_markup_to_html(markup: str) -> str:
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
    rendered = _safe_markup_text(markup)
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

    def __init__(self, user_id: int, dek: bytes, username: str | None = None):
        self.user_id = user_id
        self.username = username

        conv_dir = _conversations_dir(user_id)
        os.makedirs(conv_dir, exist_ok=True)
        # LEGACY MIGRATION — rewrite any leftover plaintext *.json files in
        # this user's directory as encrypted *.json.enc under their DEK.
        _encrypt_plaintext_conversations(conv_dir, dek)
        # END LEGACY MIGRATION

        backend = AnthropicMessagesBackend(
            system_prompt=aime_config.load_system_prompt(),
            model=aime_config.AGENT_MODEL,
            schema_files=aime_config.SCHEMA_FILES,
            conversations_dir=conv_dir,
            dek=dek,
            usage_label=username,
        )
        backend.new_session()

        gateway = ToolGateway(api_url=aime_config.API_URL, user_id=user_id)
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
        )

        # SSE plumbing: one queue per connected client, plus a replayable
        # history for refreshes. Locks are per-user so concurrent users don't
        # serialize against each other.
        self._subscribers_lock = threading.Lock()
        self._client_queues: list[queue.Queue] = []
        self._history_lock = threading.Lock()
        self._history: list[dict] = []

        # Streaming assistant text accumulator. Rich-markup tags can span
        # delta boundaries; we render to HTML once a block ends.
        self._assistant_buf: list[str] = []
        self._assistant_buf_lock = threading.Lock()

        self.controller.subscribe(self._fanout)
        self.controller.start()

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
            if payload.get("kind") not in (
                "assistant_text_delta", "assistant_text_end",
                "assistant_html_partial", "turn_end", "ready",
            ):
                self._history.append(payload)
        with self._subscribers_lock:
            targets = list(self._client_queues)
        for q in targets:
            try:
                q.put_nowait(payload)
            except queue.Full:
                pass

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
                    "text": _render_markup_to_html(full),
                })
        elif event.kind == "assistant_text" and event.text:
            self._broadcast({
                "kind": "assistant_html",
                "text": _render_markup_to_html(event.text),
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
            with _dek_cache_lock:
                dek = _dek_cache.get(user_id)
            if dek is None:
                # Should be unreachable: login_required gates the only paths
                # that reach _context_for, and it forces re-login when the
                # DEK is missing. Raise loudly if it ever happens so the
                # symptom is clear instead of a confusing decrypt failure.
                raise RuntimeError(f"no cached DEK for user {user_id}")
            ctx = UserContext(user_id, dek, g.get("username"))
            _user_contexts[user_id] = ctx
        return ctx


# ---------------------------------------------------------------------------
# HTTP app
# ---------------------------------------------------------------------------

app = Flask(__name__)
app.secret_key = _SECRET_KEY
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


def _load_page() -> str:
    with open(_PAGE_PATH) as f:
        return f.read()


# Account creation is gated by AIME_ALLOW_SIGNUP. Default is off ("0") so a
# self-hosted instance is closed by default: the admin creates accounts, then
# runs with signup disabled so nobody else can register. Set AIME_ALLOW_SIGNUP=1
# to open public registration.
_ALLOW_SIGNUP = bool(int(os.environ.get("AIME_ALLOW_SIGNUP", "0")))

# When signup is disabled, hide the "Create account" tab and form on the login
# page so visitors aren't offered something the server will reject.
_SIGNUP_DISABLED_STYLE = (
    '<style>[data-tab="signup"],form[data-form="signup"]{display:none!important}</style>'
)


def _load_login_page(login_error: str = "", signup_error: str = "") -> str:
    with open(_LOGIN_PAGE_PATH) as f:
        html = f.read()
    return (
        html
        .replace("__LOGIN_ERROR__", _h(login_error))
        .replace("__SIGNUP_ERROR__", _h(signup_error))
        .replace("__SIGNUP_DISABLED_STYLE__", "" if _ALLOW_SIGNUP else _SIGNUP_DISABLED_STYLE)
    )


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
        # DEK only lives in process memory; a server restart drops it and
        # forces re-login. The signed cookie alone never grants access to
        # encrypted data.
        with _dek_cache_lock:
            has_dek = uid in _dek_cache
        if not has_dek:
            session.clear()
            if _wants_json():
                return jsonify({"ok": False, "error": "auth required"}), 401
            return redirect(url_for("login_page"))
        g.user_id = user.id
        g.username = user.username
        return view(*args, **kwargs)
    return wrapper


@app.route("/login", methods=["GET"])
def login_page():
    if session.get("user_id"):
        return redirect("/")
    return Response(_load_login_page(), mimetype="text/html")


@app.route("/login", methods=["POST"])
def login_submit():
    username = (request.form.get("username") or "").strip()
    password = request.form.get("password") or ""
    try:
        user, dek = _auth_backend.verify(username, password)
    except _auth.AccountLocked as e:
        return Response(
            _load_login_page(login_error=str(e)),
            mimetype="text/html", status=429,
        )
    except _auth.AuthError:
        return Response(
            _load_login_page(login_error="Invalid username or password."),
            mimetype="text/html", status=401,
        )
    # Prevent session fixation: drop any prior session contents on login.
    session.clear()
    session["user_id"] = user.id
    session.permanent = True
    with _dek_cache_lock:
        _dek_cache[user.id] = dek
    return redirect("/")


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
        return Response(
            _load_login_page(signup_error="Too many signups from this address. Try again later."),
            mimetype="text/html", status=429,
        )
    username = (request.form.get("username") or "").strip()
    password = request.form.get("password") or ""
    password2 = request.form.get("password2") or ""
    if password != password2:
        return Response(
            _load_login_page(signup_error="Passwords do not match."),
            mimetype="text/html", status=400,
        )
    try:
        user, dek = _auth_backend.create(username, password)
    except _auth.UsernameTaken:
        return Response(
            _load_login_page(signup_error="That username is already taken."),
            mimetype="text/html", status=409,
        )
    except _auth.InvalidUsername as e:
        return Response(
            _load_login_page(signup_error=str(e)),
            mimetype="text/html", status=400,
        )
    except _auth.WeakPassword as e:
        return Response(
            _load_login_page(signup_error=str(e)),
            mimetype="text/html", status=400,
        )
    session.clear()
    session["user_id"] = user.id
    session.permanent = True
    with _dek_cache_lock:
        _dek_cache[user.id] = dek
    return redirect("/")


@app.route("/logout", methods=["POST"])
def logout():
    # POST-only so a stray <img src="/logout"> or prefetched link can't
    # silently log the user out. The frontend already POSTs via fetch().
    uid = session.get("user_id")
    session.clear()
    if uid is not None:
        with _dek_cache_lock:
            _dek_cache.pop(uid, None)
    return redirect(url_for("login_page"))


@app.route("/me")
@login_required
def me():
    return jsonify({"id": g.user_id, "username": g.username})


@app.route("/")
@login_required
def index():
    return Response(_load_page(), mimetype="text/html")


_ALLOWED_IMAGE_TYPES = {"image/png", "image/jpeg", "image/gif", "image/webp"}


@app.route("/send", methods=["POST"])
@login_required
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
    should_quit = ctx.controller.dispatch_input(text, images=images or None)
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
        events = _context_for(g.user_id).calendar_service.events_for_month(year, month)
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500
    return jsonify({"events": events})


@app.route("/calendar/<int:year>/<int:month>/<int:day>")
@login_required
def calendar_day(year: int, month: int, day: int):
    try:
        events = sort_events_by_date(
            _context_for(g.user_id).calendar_service.events_for_day(year, month, day)
        )
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500
    return jsonify({"events": events})


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
    try:
        _context_for(g.user_id).topic_service.replace_topic_contents(int(topic_id), contents)
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500
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
