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
import time
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
from aime.agents import (
    AgentDefinitionStore,
    AgentRunStore,
    AgentSpec,
    BackgroundAgentRunner,
    definition_to_spec,
    make_definition,
    permissions_to_allowlist,
    register as _register_agent,
)
from aime.scheduling import (
    ReminderService,
    ScheduleStore,
    Scheduler,
    make_schedule,
    render_message,
    validate_schedule,
)
from aime import auth as _auth
from aime import encryption as _enc
from aime import backup as _backup
from aime.graphics_store import (
    GraphicStore, parse_graphic_id, make_graphic_id, tag_handle_scope,
)
from aime import graphics as _graphics
from aime import email_send as _email_send
from aime import topic_shares as _topic_shares
from aime.tool_formatting import TOOL_NAME_MAP

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
# Topic-sharing grants (who may see/edit whose topics). Cross-user, so it lives
# at the root next to auth.sql, not in any one user's silo.
_share_store = _topic_shares.ShareStore(
    os.path.join(aime_config.DATABASE_DIR, "topic_shares.sql")
)


class _EditLocks:
    """In-memory advisory edit-locks for shared topics, keyed by the canonical
    (owner_id, topic_id). The first human to enter edit mode acquires the lock;
    others are kept in view mode until it's released. It is deliberately:

      * **Advisory** — it gates the *UI* (the acquire call decides the single
        winner). The PUT save path and the model never consult it, so the agent
        keeps editing freely (its edits are anchor-based and near-instant; a
        rare clobber is acceptable and the model can retry).
      * **In-memory** — locks are session state, not data. A server restart
        drops every session anyway, so dropping every lock with it is correct.
      * **TTL-backed** — a lock auto-expires so a holder who closes their tab
        without releasing (beacon lost) can't wedge the topic forever. The
        client refreshes its lock on a heartbeat well inside the TTL.
    """

    def __init__(self, ttl_seconds: float = 90.0):
        self._ttl = ttl_seconds
        self._lock = threading.Lock()
        # (owner_id, topic_id) -> (holder_user_id, expires_at_monotonic)
        self._held: dict[tuple[int, int], tuple[int, float]] = {}

    def acquire(self, owner_id: int, topic_id: int, user_id: int) -> tuple[bool, int]:
        """Try to take (or refresh, if already ours) the lock. Returns
        (granted, holder_user_id). granted is False only when someone else holds
        an unexpired lock — holder_user_id then names them."""
        now = time.monotonic()
        key = (owner_id, topic_id)
        with self._lock:
            cur = self._held.get(key)
            if cur is not None and cur[1] > now and cur[0] != user_id:
                return False, cur[0]
            self._held[key] = (user_id, now + self._ttl)
            return True, user_id

    def release(self, owner_id: int, topic_id: int, user_id: int) -> bool:
        """Release the lock if `user_id` holds it. Returns True if it did."""
        key = (owner_id, topic_id)
        with self._lock:
            cur = self._held.get(key)
            if cur is not None and cur[0] == user_id:
                del self._held[key]
                return True
            return False

    def holder(self, owner_id: int, topic_id: int) -> int | None:
        """Current holder's user id, or None if free/expired. Prunes an expired
        entry as a side effect."""
        now = time.monotonic()
        key = (owner_id, topic_id)
        with self._lock:
            cur = self._held.get(key)
            if cur is None:
                return None
            if cur[1] <= now:
                del self._held[key]
                return None
            return cur[0]


_edit_locks = _EditLocks()
_SECRET_KEY = _auth.load_or_create_secret_key(
    os.path.join(aime_config.DATABASE_DIR, "secret_key")
)
# Per-IP rate limit on /signup. Login lockout is per-account (handled inside
# the auth backend); signup needs an IP-keyed throttle since attackers don't
# pick the account name being limited.
_signup_limiter = _auth.IPRateLimiter(limit=5, window_seconds=60 * 60)

# Per-IP throttle on *failed* logins. The auth backend already locks an
# individual account after 5 bad passwords, but that's per-username — it does
# nothing against password-spraying, where one host tries one guess each across
# many accounts. This bounds total failed attempts from a single source IP.
# Only failures are counted (see login_submit), so a busy household behind one
# NAT that logs in successfully never trips it; 20 failures / 15 min is well
# above human fat-fingering but throttles an automated sprayer hard.
_login_ip_limiter = _auth.IPRateLimiter(limit=20, window_seconds=15 * 60)

# Per-pending-verification throttle on code resends, keyed by the verification
# token. Stops a logged-in-but-mid-2FA client (or signup flow) from spamming a
# victim's inbox with codes. 4 resends per 10 minutes is plenty for a slow
# inbox while capping the bombing volume.
_resend_limiter = _auth.IPRateLimiter(limit=4, window_seconds=10 * 60)

# Per-IP throttle on password-reset *requests*. Without it, anyone could mail a
# victim a stream of reset codes by repeatedly POSTing /forgot with their
# address. 5 per 15 minutes per source IP is ample for a real user.
_forgot_limiter = _auth.IPRateLimiter(limit=5, window_seconds=15 * 60)


def _user_dir(user_id: int) -> str:
    return os.path.join(aime_config.DATABASE_DIR, "users", str(user_id))


def _conversations_dir(user_id: int) -> str:
    return os.path.join(_user_dir(user_id), "conversations")


def _agent_runs_dir(user_id: int) -> str:
    """Where ``AgentRunStore`` keeps this user's encrypted background-agent run
    records — the same path the runner writes to. Mirrors the conversations
    directory but kept separate (runs never appear in the chat /load list)."""
    return os.path.join(_user_dir(user_id), "agent_runs")


def _agents_dir(user_id: int) -> str:
    """Where ``AgentDefinitionStore`` keeps this user's saved-agent definitions.
    Sibling of ``agent_runs/``: the agent's *definition* lives here, the records
    of what it did live there."""
    return os.path.join(_user_dir(user_id), "agents")


def _schedules_dir(user_id: int) -> str:
    """Where ``ScheduleStore`` keeps this user's encrypted schedule records
    (scheduled agents + event reminders). Sibling of ``agents/``."""
    return os.path.join(_user_dir(user_id), "schedules")


def _graphics_dir(user_id: int) -> str:
    """Where ``GraphicStore`` keeps this user's encrypted graphic assets — the
    canonical home for everything CreateGraphics draws, that `[graphic-…]` tags
    in chat and topics resolve against. Personal/chat graphics sit here directly;
    a topic's graphics nest under ``topic-<T>/``. Sibling of ``schedules/``."""
    return os.path.join(_user_dir(user_id), "graphics")


def _delete_topic_graphics(owner_id: int, topic_id: int) -> None:
    """Remove a topic's graphics directory (``…/graphics/topic-<T>/``) and
    everything in it. The lifecycle companion to deleting topic ``topic_id``: its
    graphics live only in this owner-scoped, per-topic dir (never in anyone's
    personal store), so dropping the dir is the whole cleanup — un-sharing touches
    no graphics, and a recipient never held a copy. Best-effort: a missing dir is
    a no-op. Call this wherever a topic is destroyed, beside
    ``_share_store.revoke_all_for_topic``."""
    if not topic_id:
        return  # topic 0 is the personal store, not a topic; never delete it
    path = os.path.join(_graphics_dir(owner_id), f"topic-{topic_id}")
    try:
        shutil.rmtree(path)
    except (OSError, FileNotFoundError):
        pass


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

# --- Markdown layer ---------------------------------------------------------
# Aime speaks Rich console markup in chat, but the model still reaches for
# Markdown for code (no Rich equivalent) and occasionally slips a `**bold**` or
# `[link](url)` in by reflex. We render both: protected constructs (code, links)
# are pulled out to placeholders that survive Rich's renderer untouched, while
# inline/Block emphasis is rewritten into the Rich tags Rich already handles.
_FENCE_RE = re.compile(r"```[ \t]*([A-Za-z0-9_+\-]*)[ \t]*\n(.*?)\n?```", re.DOTALL)
_INLINE_CODE_RE = re.compile(r"`([^`\n]+)`")
_MD_LINK_RE = re.compile(r"(?<!\\)\[([^\]\n]+)\]\((\S+?)(?:[ \t]+\"[^\"]*\")?\)")
_MD_BOLD_RE = re.compile(r"(?<!\*)\*\*(?!\s)(.+?)(?<!\s)\*\*(?!\*)", re.DOTALL)
_MD_ITALIC_RE = re.compile(r"(?<![*\w])\*(?!\s)([^*\n]+?)(?<![\s*])\*(?![*\w])")
_MD_HEADING_RE = re.compile(r"(?m)^[ \t]*(#{1,6})[ \t]+(.*?)[ \t]*#*[ \t]*$")
_MD_BULLET_RE = re.compile(r"(?m)^([ \t]*)[-*+][ \t]+")
# Only http(s) and mailto links become anchors; anything else (notably
# javascript:) stays literal text so a rendered link can never be an exploit.
_SAFE_URL_RE = re.compile(r"^(?:https?://|mailto:)", re.IGNORECASE)
# Placeholder delimiter: a private-use codepoint Rich's HTML export passes
# through verbatim (it only escapes & < >), so stashed fragments survive the
# render and can be swapped back in afterwards.
_PH = ""


# A GFM table delimiter cell: dashes with optional leading/trailing colon for
# alignment (`:---`, `:--:`, `---:`).
_TABLE_DELIM_CELL_RE = re.compile(r"^:?-+:?$")


def _split_table_row(line: str) -> list[str]:
    """Split a Markdown table row into trimmed cells on unescaped pipes,
    dropping the empty cell either side of an optional leading/trailing pipe."""
    cells = re.split(r"(?<!\\)\|", line.strip())
    if cells and cells[0].strip() == "":
        cells = cells[1:]
    if cells and cells[-1].strip() == "":
        cells = cells[:-1]
    return [c.strip().replace("\\|", "|") for c in cells]


def _table_alignments(line: str) -> list[str] | None:
    """If `line` is a valid table delimiter row, return the per-column CSS
    text-align values ("", "left", "right", "center"); otherwise None."""
    if "-" not in line or "|" not in line:
        return None
    cells = _split_table_row(line)
    if not cells:
        return None
    aligns = []
    for c in cells:
        if not _TABLE_DELIM_CELL_RE.match(c):
            return None
        left, right = c.startswith(":"), c.endswith(":")
        aligns.append("center" if left and right
                      else "right" if right else "left" if left else "")
    return aligns


def _extract_tables(markup: str, final: bool, stash) -> str:
    """Replace GFM pipe tables (a header row, a `---` delimiter row, then body
    rows) with placeholders holding rendered <table> HTML. Cell text is run
    through the full renderer so inline Rich/Markdown inside cells still works.
    Tolerant while streaming: a half-arrived table renders the rows it has."""
    lines = markup.split("\n")
    out: list[str] = []
    i, n = 0, len(lines)
    while i < n:
        line = lines[i]
        aligns = _table_alignments(lines[i + 1]) if (i + 1 < n) else None
        if "|" in line and aligns is not None:
            header = _split_table_row(line)
            body, j = [], i + 2
            while j < n and lines[j].strip() and "|" in lines[j]:
                body.append(_split_table_row(lines[j]))
                j += 1
            out.append(stash(_build_table_html(header, aligns, body, final)))
            i = j
            continue
        out.append(line)
        i += 1
    return "\n".join(out)


def _build_table_html(
    header: list[str], aligns: list[str], body: list[list[str]], final: bool
) -> str:
    cols = len(header)

    def cell(cells: list[str], idx: int, tag: str) -> str:
        text = cells[idx] if idx < len(cells) else ""
        align = aligns[idx] if idx < len(aligns) else ""
        style = f' style="text-align:{align}"' if align else ""
        return f"<{tag}{style}>{_render_markup_to_html(text, final=final)}</{tag}>"

    def row(cells: list[str], tag: str) -> str:
        return "<tr>" + "".join(cell(cells, k, tag) for k in range(cols)) + "</tr>"

    head = row(header, "th")
    rows = "".join(row(r, "td") for r in body)
    return (f'<table class="md-table"><thead>{head}</thead>'
            f"<tbody>{rows}</tbody></table>")


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


def _md_to_rich(markup: str, final: bool, stash) -> str:
    """Fold the Markdown the model emits into the Rich markup pipeline.

    Code (fenced and inline) and links can't be expressed as Rich tags, so they
    are rendered to HTML here and parked behind `stash` placeholders that pass
    through Rich untouched. Emphasis, headings and bullets are rewritten into
    the Rich tags / glyphs Rich already renders. `final` distinguishes a
    finished message from a mid-stream partial (an unclosed code fence is a
    block still being typed, not a mistake — render it as code anyway)."""
    # Fenced code first, so nothing inside it (Rich tags, ---, **, …) is touched.
    def _fence(m):
        lang = (m.group(1) or "").strip().lower()
        cls = " language-" + _h(lang) if lang else ""
        body = _h(m.group(2))
        return stash(f'<pre class="md-code"><code class="md-block{cls}">{body}'
                     "</code></pre>")
    markup = _FENCE_RE.sub(_fence, markup)
    # A fence the model has opened but not yet closed (streaming, or a final
    # message where it forgot the closer): treat the trailing remainder as code.
    if "```" in markup:
        head, _, tail = markup.rpartition("```")
        lang = ""
        nl = tail.find("\n")
        if nl != -1 and re.fullmatch(r"[A-Za-z0-9_+\-]*", tail[:nl].strip()):
            lang, tail = tail[:nl].strip().lower(), tail[nl + 1:]
        cls = " language-" + _h(lang) if lang else ""
        markup = head + stash(
            f'<pre class="md-code"><code class="md-block{cls}">'
            f'{_h(tail.rstrip(chr(10)))}</code></pre>')

    # Pipe tables → <table>. After code (so a `|` inside a fence is safe) and
    # before the inline passes (so the table's own `|`/`---`/`*` aren't mangled;
    # cell contents get the inline treatment via the recursive render instead).
    markup = _extract_tables(markup, final, stash)

    # Inline code, then links — both stashed so their contents stay literal.
    markup = _INLINE_CODE_RE.sub(
        lambda m: stash('<code class="md-inline">' + _h(m.group(1)) + "</code>"),
        markup)

    def _link(m):
        url = m.group(2)
        if not _SAFE_URL_RE.match(url):
            return m.group(0)  # leave unsafe / relative links as plain text
        return stash(f'<a href="{_h(url)}" target="_blank" '
                     f'rel="noopener noreferrer">{_h(m.group(1))}</a>')
    markup = _MD_LINK_RE.sub(_link, markup)

    # Stray inline/block Markdown → the Rich equivalents Rich can render.
    markup = _MD_BOLD_RE.sub(r"[bold]\1[/bold]", markup)
    markup = _MD_ITALIC_RE.sub(r"[italic]\1[/italic]", markup)
    markup = _MD_HEADING_RE.sub(lambda m: "[bold]" + m.group(2) + "[/bold]", markup)
    markup = _MD_BULLET_RE.sub(lambda m: m.group(1) + "• ", markup)
    return markup


def _render_markup_to_html(markup: str, final: bool = False) -> str:
    """Convert the model's chat output — Rich console markup with a Markdown
    layer on top — to inline-styled HTML.

    Rich tags drive colour/emphasis; the Markdown layer (`_md_to_rich`) adds
    code blocks, inline code and links and tolerates accidental `**bold**` /
    `# headings` / `- bullets`. Lines of only `---` become <hr> (a Markdown
    reflex the model keeps even in Rich mode).
    """
    placeholders: list[str] = []

    def stash(fragment: str) -> str:
        token = f"{_PH}{len(placeholders)}{_PH}"
        placeholders.append(fragment)
        return token

    markup = _md_to_rich(markup, final, stash)
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
    # Swap the protected code/link fragments back in. Done after the Rich render
    # so their HTML is never escaped or wrapped in style spans.
    for i, fragment in enumerate(placeholders):
        html = html.replace(f"{_PH}{i}{_PH}", fragment)
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
        from aime import messaging as _aime_messaging

        # Outbound messaging destination for this user, if they've connected one.
        # Looked up here (the auth backend is the source of truth) and handed to
        # the controller so Aime's SendMessage tool can reach the user's phone.
        _user_rec = _auth_backend.lookup(user_id)
        messaging_contact = _user_rec.messaging_contact if _user_rec else None
        # The messenger reflects server capability; the recipient is this user's
        # contact. Kept separate so the controller can distinguish "not set up on
        # the server" from "no contact connected" (see _deliver_message).
        messenger = _aime_messaging.get_messenger()
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
            # Reminder tools are client-side (handled in the controller against
            # the ScheduleStore), so they ride alongside the gateway-backed data
            # tools in the model's tool list but are never forwarded to serve.cpp.
            schema_files=(
                aime_config.SCHEMA_FILES
                + aime_config.REMINDER_SCHEMA_FILES
                + [aime_config.CREATE_GRAPHICS_SCHEMA,
                   aime_config.GET_GRAPHIC_SCHEMA]
            ),
            conversations_dir=conv_dir,
            dek=dek,
            usage_label=username,
            router=router,
            web_search_schema=(
                aime_config.WEB_SEARCH_SCHEMA if aime_config.WEB_SEARCH_ENABLED else None
            ),
            terminal_tool_schema=aime_config.ONBOARDING_TOOL_SCHEMA,
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

        # Event reminders the model sets go through the same ScheduleStore the
        # event-modal UI and the scheduler loop use, linked to events by id.
        # Events are looked up via the shared horizon helper so a reminder can be
        # set against anything the user has coming up.
        reminder_service = ReminderService(
            ScheduleStore(_schedules_dir(user_id), dek),
            lambda: _scheduler_upcoming_events(user_id),
        )

        # Bridge giving the model cross-user record access: shared topics (both
        # directions) plus the post-write self-clear. See _RecordSyncBridge.
        # Bound to this user_id for the context's life.
        self.record_sync = _RecordSyncBridge(user_id)

        self.controller = ConversationController(
            backend=backend,
            tool_gateway=gateway,
            worker_spawner=spawn_worker,
            web_search_agent=web_search_agent,
            messenger=messenger,
            message_recipient=messaging_contact,
            reminder_service=reminder_service,
            record_sync=self.record_sync,
            graphic_store_provider=_make_graphic_store_provider(user_id),
        )

        # Seed the session with the user's last-seen zone so any turn that runs
        # before the first /send (notably onboarding's opening turn) stamps the
        # right local "now". Each /send still refreshes it, so a traveling user
        # self-corrects on their next message.
        if _user_rec and _user_rec.tz:
            self.controller.set_client_timezone(_user_rec.tz)

        # Likewise seed the date/time display preferences so a pre-/send turn
        # (onboarding) already writes dates in the user's format. Refreshed on
        # each /send.
        if _user_rec and (_user_rec.date_format or _user_rec.time_format):
            self.controller.set_client_date_prefs(
                _user_rec.date_format, _user_rec.time_format
            )

        self.controller.subscribe(self._fanout)
        self.controller.start()

        # Tracks records the user has edited via the UI since the model last
        # took a turn. Drained as a compact <stale> tag on the next /send so
        # the model knows its earlier view of those records is out of date.
        # Tuple is (kind, id, title); kind is "topic" or "event".
        self._stale_lock = threading.Lock()
        self._stale_records: list[tuple[str, int, str]] = []

        # Last IANA timezone we persisted for this user (see /send). Seeded from
        # the stored value so we only write to the DB when the browser reports a
        # *changed* zone, not on every message.
        self._persisted_tz = _user_rec.tz if _user_rec else None
        # Same change-detection for the date/time display prefs (see /send).
        self._persisted_date_prefs = (
            (_user_rec.date_format, _user_rec.time_format) if _user_rec
            else (None, None)
        )

    # ---- Stale-record tracking --------------------------------------------

    def mark_record_stale(self, kind: str, record_id: int | str, title: str) -> None:
        """Note that a record this user's model has seen just changed (a UI edit,
        or a change pushed in from the other side of a shared topic), so the next
        user turn carries a <stale> tag and the model knows to re-read it. Dedupes
        on (kind, id) so repeated edits to the same record only cost one entry.

        `record_id` is normally the bare integer id, but for a topic shared *to*
        this user it's the composite "<owner>:<topic>" handle string — i.e. the
        id their model actually addresses the topic by, so the tag lines up."""
        title = (title or "").strip()
        with self._stale_lock:
            self._stale_records = [
                e for e in self._stale_records
                if not (e[0] == kind and e[1] == record_id)
            ]
            self._stale_records.append((kind, record_id, title))

    def clear_record_stale(self, kind: str, record_id: int | str) -> None:
        """Drop a pending stale flag for (kind, id). Used when this user's own
        model just wrote the record: the cross-user mutation choke point flags
        every party (this user included), but the model already holds the fresh
        content, so re-reading would be wasted — remove its own flag."""
        with self._stale_lock:
            self._stale_records = [
                e for e in self._stale_records
                if not (e[0] == kind and e[1] == record_id)
            ]

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
                "remote_edit", "agent_run_update", "share_update",
                "topic_lock",
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

    def notify_topic_lock(self, payload: dict) -> None:
        """Broadcast an edit-lock state change (someone started/stopped editing
        a shared topic) to this user's sessions so their Edit affordance updates
        live. `payload` carries kind=topic_lock, owner_id, topic_id, locked,
        locked_by."""
        self._broadcast(payload)

    def notify_share_update(self) -> None:
        """Tell every connected session of this user that their set of topic
        shares changed (a share was offered to them, accepted/declined,
        revoked, or a shared topic was edited), so any open topics pane
        re-fetches and the in-app notification surfaces. Fired cross-user — the
        sharing routes push this into the *recipient's* (or owner's) context."""
        self._broadcast({"kind": "share_update"})

    def notify_agent_run_update(self) -> None:
        """Tell every connected session of this user that the set of stored
        background-agent runs changed (a run just started or finished), so any
        open agent/conversations pane re-fetches. Fired by the ad-hoc run
        thread, which has no other path into the SSE fanout."""
        self._broadcast({"kind": "agent_run_update"})

    def _on_backend_mutation(self, tool_name: str, payload: dict | None = None) -> None:
        # Fired by ToolGateway after any successful non-read tool call, covering
        # both AI tool calls and direct UI service calls. Refresh this user's own
        # open tabs unconditionally.
        self.notify_remote_edit(tool_name)
        # If the write changed a tracked record (topic content, event, …), fan a
        # single notification out to everyone who can see it — each party's model
        # gets a <stale> tag on its next turn and their live UI re-fetches. This
        # is the one choke point for record sync: every write flows through here,
        # and self.user_id is always the record's *owner* (a shared-topic write
        # is routed through the owner's gateway, so this runs on their context),
        # so the payload id is the bare owner-side id.
        kind = _RECORD_KIND_BY_TOOL.get(tool_name)
        record_id = payload.get("id") if isinstance(payload, dict) else None
        if kind is not None and record_id is not None:
            _propagate_record_change(kind, self.user_id, record_id, payload)

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
        # Structured payload for events that carry one (e.g. `graphic`, which
        # holds {format, summary, source} for the frontend to render).
        if event.payload is not None:
            payload["payload"] = event.payload
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
# Whether the browser-facing connection is HTTPS — which decides the cookie
# `Secure` flag and HSTS (see _security_headers and the trusted-device cookie).
# This is distinct from whether *this process* terminates TLS (AIME_HTTPS): a
# reverse proxy commonly terminates TLS and forwards plain HTTP to us, so the
# connection is still HTTPS from the browser's view even with AIME_HTTPS=0.
# Defaults to on whenever the app serves its own TLS; behind a TLS-terminating
# proxy set AIME_SECURE_COOKIES=1 (the docker/.env defaults do this). Turn it
# off only for genuinely plain-HTTP access over a non-localhost address.
# (_env_bool is defined further down; inline the same parse to avoid a
# forward reference at module-load time.)
_SECURE_COOKIES = os.environ.get(
    "AIME_SECURE_COOKIES",
    "1" if int(os.environ.get("AIME_HTTPS", "0")) else "0",
).strip() not in ("", "0", "false", "False", "no")

# Session cookie hardening. `Secure`/HSTS follow _SECURE_COOKIES so they stay
# on behind a TLS proxy (AIME_HTTPS=0) while local plain-HTTP dev still works.
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Strict",
    SESSION_COOKIE_SECURE=_SECURE_COOKIES,
    PERMANENT_SESSION_LIFETIME=60 * 60 * 24 * 14,  # 14 days
    # Cap request bodies. Attachments (images, audio) flow through /send and
    # /transcribe; 32 MiB is comfortably above realistic use and bounds the
    # damage from a malicious client uploading multi-GB payloads.
    MAX_CONTENT_LENGTH=32 * 1024 * 1024,
)


# "Remember this device" cookie. Holds a trusted-device token (the raw value;
# only its hash is stored server-side) that lets a device skip login 2FA until
# the token expires. Separate from the session cookie so it survives logout —
# that's the point: the device stays trusted, the session does not. Hardened
# the same way as the session cookie (HttpOnly, SameSite, Secure-when-HTTPS).
_TRUSTED_DEVICE_COOKIE = "aime_td"


def _issue_trusted_device(resp: Response, user_id: int) -> Response:
    """Mint a trusted-device token for `user_id` and attach it to `resp` as the
    remember-this-device cookie. Called from the verify handlers when the user
    ticks "trust this device". Returns the same response for chaining."""
    token, expires_at = _auth_backend.create_trusted_device(
        user_id, user_agent=request.headers.get("User-Agent"),
    )
    resp.set_cookie(
        _TRUSTED_DEVICE_COOKIE, token,
        max_age=max(0, expires_at - int(time.time())),
        httponly=True,
        secure=bool(app.config.get("SESSION_COOKIE_SECURE")),
        samesite="Strict",
    )
    return resp


def _wants_trusted_device() -> bool:
    """True if the current form post asked to remember this device."""
    return bool(request.form.get("trust_device"))


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
    # HSTS: only emit when we're actually serving over TLS (AIME_HTTPS=1). Sent
    # on plain http it would be ignored by browsers, but advertising it only
    # under TLS keeps the contract honest. Two years + subdomains is the
    # commonly-recommended baseline; preload is left off so the operator opts
    # into that irreversible step deliberately.
    if app.config.get("SESSION_COOKIE_SECURE"):
        resp.headers.setdefault(
            "Strict-Transport-Security",
            "max-age=63072000; includeSubDomains",
        )
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
_FORGOT_PAGE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "resources", "style", "forgot_password.html",
)
_RESET_PAGE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "resources", "style", "reset_password.html",
)


def _load_page() -> str:
    with open(_PAGE_PATH) as f:
        return f.read()


def _load_forgot_page(error: str = "") -> str:
    with open(_FORGOT_PAGE_PATH) as f:
        html = f.read()
    return html.replace("__ERROR__", _h(error))


def _load_reset_page(error: str = "", notice: str = "") -> str:
    with open(_RESET_PAGE_PATH) as f:
        html = f.read()
    return html.replace("__ERROR__", _h(error)).replace("__NOTICE__", _h(notice))


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
    'label[for="signup-email"] + #signup-email + .hint,'
    # Password reset needs an email on file, which only exists when email 2FA
    # is on — hide the "Forgot password?" link otherwise.
    '[data-forgot]'
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
    signup_first_name: str = "",
    signup_last_name: str = "",
) -> str:
    """Render the login page.

    `notice` shows an informational line above the sign-in form (e.g. after a
    recovery). `recover_username`, when set, switches the page into its
    account-recovery prompt for that account. `login_username` pre-fills the
    sign-in username field; `signup_username` / `signup_email` /
    `signup_first_name` / `signup_last_name` pre-fill the signup form after a
    validation error so the user doesn't retype them.
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
        .replace("__SIGNUP_FIRST_NAME__", _h(signup_first_name))
        .replace("__SIGNUP_LAST_NAME__", _h(signup_last_name))
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
    stale_login = session.pop("pending_login_token", None)
    if stale_login:
        _auth_backend.cancel_verification(stale_login)
    session.pop("pending_email_user_id", None)
    session.pop("pending_email_was_reinitialized", None)
    return Response(_load_login_page(), mimetype="text/html")


def _grant_full_session(user, was_reinitialized: bool) -> Response:
    """Promote a verified login into a full session and return the redirect to
    the app. Shared by the no-2FA path and the trusted-device bypass.

    If verify() just auto-upgraded a pre-v2 account it minted a fresh DEK,
    leaving any stored conversation files unreadable — wipe them the same way
    the legacy migration path does. (See LEGACY MIGRATION AUTH.)"""
    session["user_id"] = user.id
    session.permanent = True
    if was_reinitialized:
        g.username = user.username
        g.user_id = user.id
        _context_for(user.id).controller.delete_all_sessions()
    return redirect("/")


@app.route("/login", methods=["POST"])
def login_submit():
    ip = request.remote_addr or "unknown"
    # Per-IP brute-force guard: if this source has already burned through the
    # failed-login budget, reject *before* the expensive Argon2 verify so a
    # sprayer can't keep us hashing. Checked without recording a hit (only
    # actual failures below count toward the budget).
    if _login_ip_limiter.blocked(ip):
        _auth_backend.log_event(_auth.EVENT_LOGIN_IP_THROTTLED, ip=ip)
        return Response(
            _load_login_page(
                login_error="Too many failed attempts from your network. "
                "Please wait a few minutes and try again.",
            ),
            mimetype="text/html", status=429,
        )
    username = (request.form.get("username") or "").strip()
    password = request.form.get("password") or ""
    try:
        user, _dek, was_reinitialized = _auth_backend.verify(
            username, password, ip=ip,
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
        # Count this failure against the source IP's budget (separate from the
        # backend's per-account lockout, which can't see a spray across many
        # usernames). The error stays generic to avoid a username oracle.
        _login_ip_limiter.hit(ip)
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
    # Full login 2FA: the password was correct and the account already has an
    # email on file, so mail a one-time code to it and withhold the session
    # until that code is entered. The verified user_id is parked in
    # pending_email_user_id (not user_id) so nothing treats them as logged in
    # mid-challenge; the shared add-email finalizer promotes it on success.
    if _DO_EMAIL_VERIFICATION:
        # Remember-this-device: if this browser still holds a valid
        # trusted-device token for the account, the second factor was already
        # satisfied here recently — skip the emailed code and log straight in.
        td_token = request.cookies.get(_TRUSTED_DEVICE_COOKIE)
        if td_token and _auth_backend.is_trusted_device(user.id, td_token):
            return _grant_full_session(user, was_reinitialized)
        def _challenge_failed():
            # Either the stored address is unusable or mail is down. With 2FA
            # required we can't safely grant the session, so block the login
            # with a calm message rather than bypassing the second factor.
            return Response(
                _load_login_page(
                    login_error="We couldn't send your sign-in code right now. "
                    "Please try again in a moment.",
                ),
                mimetype="text/html", status=502,
            )
        try:
            token, code, _norm = _auth_backend.start_login_verification(
                user.id, user.email,
            )
        except _auth.InvalidEmail:
            return _challenge_failed()
        try:
            _email_send.send_verification_code(user.email, code)
        except _email_send.EmailSendError:
            # Drop the pending row whose code never made it out the door.
            _auth_backend.cancel_verification(token)
            return _challenge_failed()
        session["pending_email_user_id"] = user.id
        session["pending_login_token"] = token
        if was_reinitialized:
            session["pending_email_was_reinitialized"] = True
        return redirect(url_for("login_verify_page"))
    # No email on file path is handled above; here the account simply has email
    # 2FA disabled server-wide. Grant the session (wiping stale data if verify()
    # just re-keyed a legacy account).
    return _grant_full_session(user, was_reinitialized)


# ---------------------------------------------------------------------------
# Login 2FA: code-entry gate for accounts that already have an email on file
# ---------------------------------------------------------------------------
#
# Reached only from login_submit after a correct password. The challenge state
# lives in pending_email_user_id (+ pending_login_token); the session is not
# granted until the mailed code is confirmed, at which point the shared
# _finalize_pending_email_login() promotes it. Mirrors the add-email/verify
# routes, minus the email-collection step (the address is already known).


def _login_verify_render(email: str, **kwargs) -> Response:
    """Render the shared code-entry page wired to the login-2FA endpoints."""
    return Response(
        _load_verify_page(
            email=email,
            verify_action=url_for("login_verify_submit"),
            resend_action=url_for("login_verify_resend"),
            cancel_href=url_for("login_verify_cancel"),
            purpose_phrase="finish signing in",
            **kwargs,
        ),
        mimetype="text/html",
    )


@app.route("/login/verify", methods=["GET"])
def login_verify_page():
    uid = session.get("pending_email_user_id")
    token = session.get("pending_login_token")
    if not uid or not token:
        return redirect(url_for("login_page"))
    email = _auth_backend.verification_email(token)
    if email is None:
        # Code expired before it was entered — make them sign in again.
        session.pop("pending_login_token", None)
        return redirect(url_for("login_page"))
    return _login_verify_render(email)


@app.route("/login/verify", methods=["POST"])
def login_verify_submit():
    uid = session.get("pending_email_user_id")
    token = session.get("pending_login_token")
    if not uid or not token:
        return redirect(url_for("login_page"))
    code = (request.form.get("code") or "").strip()
    try:
        _auth_backend.complete_login_verification(token, code)
    except _auth.VerificationError as e:
        email = _auth_backend.verification_email(token)
        if email is None:
            # Attempts exhausted or expired — back to the login page.
            session.pop("pending_login_token", None)
            session.pop("pending_email_user_id", None)
            session.pop("pending_email_was_reinitialized", None)
            return Response(
                _load_login_page(
                    login_error="That code expired or was used up. "
                    "Please sign in again.",
                ),
                mimetype="text/html", status=400,
            )
        return _login_verify_render(email, error=str(e))
    _finalize_pending_email_login()
    resp = redirect("/")
    # The second factor was just satisfied — honor "trust this device" so the
    # next login on this browser can skip the emailed code.
    if _wants_trusted_device():
        _issue_trusted_device(resp, uid)
    return resp


@app.route("/login/verify/resend", methods=["POST"])
def login_verify_resend():
    if not session.get("pending_email_user_id"):
        return redirect(url_for("login_page"))
    token = session.get("pending_login_token")
    if not token:
        return redirect(url_for("login_page"))
    if not _resend_limiter.hit(token):
        email = _auth_backend.verification_email(token)
        if email is None:
            return redirect(url_for("login_page"))
        resp = _login_verify_render(
            email, error="Please wait a moment before requesting another code.",
        )
        resp.status_code = 429
        return resp
    fresh = _auth_backend.resend_verification_code(token)
    if fresh is None:
        session.pop("pending_login_token", None)
        return redirect(url_for("login_page"))
    code, email = fresh
    try:
        _email_send.send_verification_code(email, code)
    except _email_send.EmailSendError as e:
        return _login_verify_render(email, error=str(e))
    return _login_verify_render(email, notice="We sent a fresh code.")


@app.route("/login/verify/cancel", methods=["GET"])
def login_verify_cancel():
    token = session.pop("pending_login_token", None)
    session.pop("pending_email_user_id", None)
    session.pop("pending_email_was_reinitialized", None)
    if token:
        _auth_backend.cancel_verification(token)
    return redirect(url_for("login_page"))


# ---------------------------------------------------------------------------
# Forgot-password / reset flow
# ---------------------------------------------------------------------------
#
# A reset emails a 6-digit code to the address already on file for the account
# (never to a caller-supplied address), then lets the holder of that code set a
# new password. Everything is enumeration-safe: whether or not an account
# matches, the user lands on the same code-entry page and sees the same copy,
# so the flow never reveals which usernames/emails exist. State is two signed
# session keys: `reset_in_progress` (a marker set for every request, including
# decoys) and `pending_reset_token` (only present when a real account matched).
# Only available when email 2FA is configured — without an email on file there
# is nowhere to send the code.


@app.route("/forgot", methods=["GET"])
def forgot_page():
    if not _DO_EMAIL_VERIFICATION:
        return redirect(url_for("login_page"))
    return Response(_load_forgot_page(), mimetype="text/html")


@app.route("/forgot", methods=["POST"])
def forgot_submit():
    if not _DO_EMAIL_VERIFICATION:
        return redirect(url_for("login_page"))
    ip = request.remote_addr or "unknown"
    identifier = (request.form.get("identifier") or "").strip()
    # Drop any half-finished prior reset before starting a new one.
    stale = session.pop("pending_reset_token", None)
    if stale:
        _auth_backend.cancel_verification(stale)
    # Mark the flow in progress regardless of outcome so the next page renders
    # identically whether or not an account matched (enumeration-safe).
    session["reset_in_progress"] = True
    # Throttle reset requests per IP so the form can't be used to bomb a
    # victim's inbox. Over the limit we silently skip sending but still advance
    # to the same page, so a prober can't distinguish throttle from no-match.
    if _forgot_limiter.hit(ip):
        result = _auth_backend.start_password_reset(identifier)
        if result is not None:
            token, code, email = result
            try:
                _email_send.send_verification_code(email, code)
                session["pending_reset_token"] = token
            except _email_send.EmailSendError:
                # Mail down — drop the pending row. Stay enumeration-safe by
                # not surfacing the failure; the user can retry.
                _auth_backend.cancel_verification(token)
    return redirect(url_for("reset_password_page"))


@app.route("/forgot/verify", methods=["GET"])
def reset_password_page():
    if not _DO_EMAIL_VERIFICATION:
        return redirect(url_for("login_page"))
    if not session.get("reset_in_progress"):
        return redirect(url_for("forgot_page"))
    return Response(_load_reset_page(), mimetype="text/html")


@app.route("/forgot/verify", methods=["POST"])
def reset_password_submit():
    if not _DO_EMAIL_VERIFICATION:
        return redirect(url_for("login_page"))
    if not session.get("reset_in_progress"):
        return redirect(url_for("forgot_page"))
    code = (request.form.get("code") or "").strip()
    new_password = request.form.get("password") or ""
    confirm = request.form.get("password2") or ""
    token = session.get("pending_reset_token")

    def _err(msg: str, status: int = 400):
        return Response(
            _load_reset_page(error=msg), mimetype="text/html", status=status,
        )

    if new_password != confirm:
        return _err("Those passwords don't match.")
    if not token:
        # Decoy path: no eligible account matched (or mail failed). Fail the
        # same way a wrong code would, so existence is never revealed.
        return _err("That code is invalid or has expired. Please start over.")
    try:
        user_id = _auth_backend.complete_password_reset(
            token, code, new_password,
        )
    except _auth.WeakPassword as e:
        return _err(str(e))
    except _auth.VerificationError as e:
        # If the pending row is gone (expired / too many attempts) there's
        # nothing to retry against — send them back to request a fresh code.
        if _auth_backend.verification_email(token) is None:
            session.pop("pending_reset_token", None)
            session.pop("reset_in_progress", None)
            return Response(
                _load_login_page(
                    notice="Your reset code expired. Please request a new one.",
                ),
                mimetype="text/html", status=400,
            )
        return _err(str(e))
    # Success. Revoke every trusted device so the reset actually locks out
    # anyone holding a remember-this-device cookie, clear the flow state, and
    # send them to sign in fresh with the new password (we deliberately don't
    # auto-login).
    _auth_backend.revoke_all_trusted_devices(user_id)
    session.pop("pending_reset_token", None)
    session.pop("reset_in_progress", None)
    # Name the account that was actually reset so the user is never left
    # guessing which one changed (the code proved control of its inbox, so
    # this reveals nothing they couldn't already see). Guards against the
    # confusing case where the entered email belonged to a different account
    # than the one they had in mind.
    reset_user = _auth_backend.lookup(user_id)
    who = f" for {reset_user.username}" if reset_user else ""
    return Response(
        _load_login_page(
            notice=f"Your password has been updated{who}. Please sign in.",
        ),
        mimetype="text/html",
    )


@app.route("/forgot/verify/resend", methods=["POST"])
def reset_password_resend():
    if not _DO_EMAIL_VERIFICATION:
        return redirect(url_for("login_page"))
    if not session.get("reset_in_progress"):
        return redirect(url_for("forgot_page"))
    token = session.get("pending_reset_token")
    if not token:
        # Decoy: no real pending reset, but answer identically.
        return Response(
            _load_reset_page(
                notice="If that account exists, we've sent a new code.",
            ),
            mimetype="text/html",
        )
    if not _resend_limiter.hit(token):
        return Response(
            _load_reset_page(
                error="Please wait a moment before requesting another code.",
            ),
            mimetype="text/html", status=429,
        )
    fresh = _auth_backend.resend_verification_code(token)
    if fresh is None:
        session.pop("pending_reset_token", None)
        return Response(
            _load_reset_page(
                error="That code expired. Please start over.",
            ),
            mimetype="text/html", status=400,
        )
    code, email = fresh
    try:
        _email_send.send_verification_code(email, code)
    except _email_send.EmailSendError:
        return Response(
            _load_reset_page(
                error="We couldn't send the code right now. Please try again.",
            ),
            mimetype="text/html", status=502,
        )
    return Response(
        _load_reset_page(notice="We sent a fresh code."),
        mimetype="text/html",
    )


@app.route("/forgot/cancel", methods=["GET"])
def reset_password_cancel():
    token = session.pop("pending_reset_token", None)
    session.pop("reset_in_progress", None)
    if token:
        _auth_backend.cancel_verification(token)
    return redirect(url_for("login_page"))


# ---------------------------------------------------------------------------
# Add-email-to-existing-account flow (post-login gate for legacy accounts)
# ---------------------------------------------------------------------------


def _finalize_pending_email_login() -> None:
    """Promote the pending_email_* session state into a full login, wiping
    any one-shot flags. Called once the email verification succeeds."""
    uid = session.pop("pending_email_user_id", None)
    was_reinit = session.pop("pending_email_was_reinitialized", False)
    session.pop("pending_email_token", None)
    session.pop("pending_login_token", None)
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
    resp = redirect("/")
    # Email is now verified; honor "trust this device" so the first real login
    # after setup can skip the 2FA code on this browser.
    if _wants_trusted_device():
        _issue_trusted_device(resp, uid)
    return resp


@app.route("/add-email/verify/resend", methods=["POST"])
def add_email_verify_resend():
    if not session.get("pending_email_user_id"):
        return redirect(url_for("login_page"))
    token = session.get("pending_email_token")
    if not token:
        return redirect(url_for("add_email_page"))
    if not _resend_limiter.hit(token):
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
                error="Please wait a moment before requesting another code.",
            ),
            mimetype="text/html", status=429,
        )
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
    # Display-only real name (optional). Stored verbatim; the username remains
    # the immutable identity that keys everything.
    first_name = (request.form.get("first_name") or "").strip()
    last_name = (request.form.get("last_name") or "").strip()

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
                signup_first_name=first_name,
                signup_last_name=last_name,
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
                username, password, api_access=(_ACCESS_MODE == "open"),
                first_name=first_name, last_name=last_name,
            )
        except _auth.UsernameTaken:
            return _signup_err("That username is already taken.", status=409)
        except _auth.InvalidUsername as e:
            return _signup_err(str(e))
        except _auth.WeakPassword as e:
            return _signup_err(str(e))
        except _auth.InvalidName as e:
            return _signup_err(str(e))
        session.clear()
        session["user_id"] = user.id
        session.permanent = True
        return redirect("/")

    try:
        token, code, _email_norm = _auth_backend.start_signup_verification(
            username, password, email,
            api_access=(_ACCESS_MODE == "open"),
            first_name=first_name, last_name=last_name,
        )
    except _auth.UsernameTaken:
        return _signup_err("That username is already taken.", status=409)
    except _auth.InvalidUsername as e:
        return _signup_err(str(e))
    except _auth.WeakPassword as e:
        return _signup_err(str(e))
    except _auth.InvalidEmail as e:
        return _signup_err(str(e))
    except _auth.InvalidName as e:
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
    resp = redirect("/")
    # The account's email is verified as of now; honor "trust this device" so
    # this browser skips 2FA on the next login.
    if _wants_trusted_device():
        _issue_trusted_device(resp, user.id)
    return resp


@app.route("/signup/verify/resend", methods=["POST"])
def signup_verify_resend():
    token = session.get("pending_signup_token")
    if not token:
        return redirect(url_for("login_page"))
    if not _resend_limiter.hit(token):
        email = _auth_backend.verification_email(token)
        if email is None:
            session.pop("pending_signup_token", None)
            return redirect(url_for("login_page"))
        return Response(
            _load_verify_page(
                email=email,
                verify_action=url_for("signup_verify_submit"),
                resend_action=url_for("signup_verify_resend"),
                cancel_href=url_for("signup_verify_cancel"),
                purpose_phrase="finish creating your account",
                error="Please wait a moment before requesting another code.",
            ),
            mimetype="text/html", status=429,
        )
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
        "messaging_contact": user.messaging_contact if user else None,
        "first_name": user.first_name if user else None,
        "last_name": user.last_name if user else None,
        "access_mode": _ACCESS_MODE,
        "api_access": g.api_access,
    })


@app.route("/display-name", methods=["POST"])
@login_required
def display_name():
    """Set (or clear, with blanks) the account's display-only first/last name.
    The username is intentionally not editable — it's the immutable identity
    that keys all of the user's data. These names are cosmetic only."""
    data = request.get_json(silent=True) or {}
    first_name = (data.get("first_name") or "").strip() or None
    last_name = (data.get("last_name") or "").strip() or None
    try:
        _auth_backend.set_display_name(g.user_id, first_name, last_name)
    except _auth.InvalidName as e:
        return jsonify({"ok": False, "error": "invalid", "message": str(e)}), 400
    user = _auth_backend.lookup(g.user_id)
    return jsonify({
        "ok": True,
        "first_name": user.first_name if user else first_name,
        "last_name": user.last_name if user else last_name,
    })


@app.route("/messaging-contact", methods=["POST"])
@login_required
def messaging_contact():
    """Connect (or clear, with an empty value) the account's outbound-messaging
    destination — the chat id / number Aime and background agents text via
    aime.messaging. An advanced-settings convenience; the value is opaque to the
    server and just stored. Takes effect on the user's next session (the live
    controller reads its recipient at construction)."""
    data = request.get_json(silent=True) or {}
    contact = (data.get("contact") or "").strip() or None
    _auth_backend.set_messaging_contact(g.user_id, contact)
    # Push it into the live session too (if one is cached) so it works right
    # away rather than only after the next login rebuilds the controller.
    ctx = _user_contexts.get(g.user_id)
    if ctx is not None:
        from aime import messaging as _aime_messaging
        ctx.controller.set_messaging_target(_aime_messaging.get_messenger(), contact)
    return jsonify({"ok": True, "messaging_contact": contact})


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
        # Persist the zone so code with no live client (background agent runs)
        # has a sane fallback. Only write when it actually changed, so the
        # common case (same zone every message) costs nothing.
        if tz != ctx._persisted_tz:
            if _auth_backend.set_timezone(g.user_id, tz):
                ctx._persisted_tz = tz
    # Date/time display preferences ride along the same way (the browser
    # resolves its "auto" option to a concrete value before sending). They tell
    # the model which format to write dates/times in. Persisted like the zone so
    # a clientless background run honors an explicit choice; only on change.
    df = data.get("date_format")
    tf = data.get("time_format")
    df = df if isinstance(df, str) and df else None
    tf = tf if isinstance(tf, str) and tf else None
    ctx.controller.set_client_date_prefs(df, tf)
    if (df, tf) != ctx._persisted_date_prefs:
        if _auth_backend.set_date_prefs(g.user_id, df, tf):
            ctx._persisted_date_prefs = (df, tf)
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
                # Everything in the snapshot is history relative to *this*
                # connection, even live events from earlier in the session. Force
                # from_replay so a reconnect (phone wake, network change) renders
                # them instantly instead of re-running the typewriter / re-flipping
                # the busy gate. The client also rebuilds its transcript on
                # reconnect, so these can't duplicate what's already on screen.
                if not payload.get("from_replay"):
                    payload = {**payload, "from_replay": True}
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
                try:
                    payload = q.get(timeout=20)
                except queue.Empty:
                    # Heartbeat. Without this, an idle stream parks this
                    # worker thread in q.get() forever, and a client that
                    # died silently (phone sleep, closed tab, network drop,
                    # proxy idle timeout) is never noticed because we only
                    # learn the socket is dead when a yield tries to write to
                    # it. Emitting a keepalive comment on idle forces that
                    # write: if the peer is gone it raises here, the `finally`
                    # runs, and the waitress thread is reclaimed instead of
                    # leaking. (16 threads leak over ~a day → queue backs up.)
                    yield ": keepalive\n\n"
                    continue
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


# --- Background-agent runs -------------------------------------------------
# Read-only audit trail of what every background agent did for this user. The
# frontend only surfaces these to Verbose users (the same tier that sees tool
# chatter), folding them into the Conversations menu. Runs are encrypted with
# the user's DEK, exactly like conversations, so we decrypt on demand here.


def _agent_run_store(user_id: int) -> AgentRunStore:
    return AgentRunStore(_agent_runs_dir(user_id), _auth_backend.get_dek(user_id))


def _agent_def_store(user_id: int) -> AgentDefinitionStore:
    return AgentDefinitionStore(_agents_dir(user_id), _auth_backend.get_dek(user_id))


def _schedule_store(user_id: int) -> ScheduleStore:
    return ScheduleStore(_schedules_dir(user_id), _auth_backend.get_dek(user_id))


# --- Scheduler wiring ------------------------------------------------------
# The scheduler core (aime.scheduling) is deliberately ignorant of this app: it
# fires due records by calling back into these three helpers. They are the only
# bridge between the headless loop and the request-time machinery (agent runs,
# messaging, the events backend).


def _scheduler_run_agent(agent_id: str, user_id: int, tz: str | None) -> None:
    """Scheduler action: run a saved agent now, exactly as its Run button does.

    Honors the same api-access gate as ``/agents/<id>/run`` — a scheduled run
    spends tokens, so an account without send access must not fire one. A
    deleted agent is a quiet no-op (the user can remove the dangling schedule)."""
    rec = _auth_backend.lookup(user_id)
    if rec is None or not rec.api_access:
        return
    record = _agent_def_store(user_id).load(agent_id)
    if record is None:
        return
    _launch_agent_run(definition_to_spec(record), user_id, tz, agent_id=agent_id)


def _scheduler_send_message(user_id: int, text: str) -> None:
    """Scheduler action: deliver a reminder to the user's stored contact over the
    configured channel. Best-effort — messaging off, no contact connected, or a
    transport hiccup all degrade to a quiet skip rather than a hard failure."""
    from aime import messaging as _aime_messaging
    messenger = _aime_messaging.get_messenger()
    if messenger is None:
        return
    rec = _auth_backend.lookup(user_id)
    contact = rec.messaging_contact if rec else None
    if not contact:
        return
    try:
        messenger.send(contact, text)
    except _aime_messaging.MessageSendError as exc:
        app.logger.warning("scheduled reminder to user %s failed: %s", user_id, exc)


def _scheduler_upcoming_events(user_id: int) -> list:
    """Active events from today out to the reminder horizon (~400 days, past the
    366-day ``days_before`` cap), for the scheduler's event reminders. Built off
    any request, so it constructs its own gateway rather than a UserContext.

    Returning the full horizon is a correctness contract: a relative schedule
    whose linked event isn't in this list is treated as an orphan and deleted,
    so the window must be wide enough to always include an armed reminder's
    event (see docs/scheduling.md §8)."""
    gw = ToolGateway(api_url=aime_config.API_URL, user_id=user_id)
    today = datetime.date.today()
    end = today + datetime.timedelta(days=400)
    return CalendarService(gw).events_in_range(
        today.strftime("%d/%m/%Y"), end.strftime("%d/%m/%Y")
    )


def _dispatch_schedule_now(user_id: int, record: dict, tz: str | None) -> None:
    """Fire a schedule's action immediately (the manual /run path), mirroring
    what the loop's ``_fire`` does — including resolving the linked event so a
    relative reminder's template renders the same way it would on a real fire.
    Works even when the background loop is disabled."""
    action = record.get("action", {})
    if action.get("kind") == "run_agent":
        _scheduler_run_agent(action.get("agent_id"), user_id, tz)
        return
    event = None
    trigger = record.get("trigger", {})
    if trigger.get("kind") == "relative" and trigger.get("event_id") is not None:
        events = {e["id"]: e for e in _scheduler_upcoming_events(user_id) if "id" in e}
        event = events.get(trigger["event_id"])
    _scheduler_send_message(user_id, render_message(action.get("message", ""), event))


_scheduler: Scheduler | None = None


def _start_scheduler() -> None:
    """Boot the background scheduler thread once, unless disabled via
    ``AIME_SCHEDULER=0`` (tests, or a deploy that runs the loop elsewhere)."""
    global _scheduler
    if os.environ.get("AIME_SCHEDULER", "1").strip().lower() in ("0", "false", "no", "off"):
        return
    if _scheduler is not None:
        return
    _scheduler = Scheduler(
        auth=_auth_backend,
        schedules_dir=_schedules_dir,
        run_agent=_scheduler_run_agent,
        send_message=_scheduler_send_message,
        upcoming_events=_scheduler_upcoming_events,
    )
    _scheduler.start()


# In-flight background-agent runs, in memory only. A run record isn't written
# until the run *finishes*, so without this the UI has no way to show that a run
# is underway — which reads as "nothing happened" when you press Run. We track
# every launched run here (keyed user_id -> token -> descriptor) for the brief
# window it's executing, surface it as a "running" entry, and drop it the moment
# the run lands its record. Safe as a plain dict + lock because the server is a
# single process; it's intentionally ephemeral (a restart clears it, which is
# correct — those threads died with the process).
_active_agent_runs: dict[int, dict[str, dict]] = {}
_active_runs_lock = threading.Lock()


def _add_active_run(user_id: int, descriptor: dict) -> None:
    with _active_runs_lock:
        _active_agent_runs.setdefault(user_id, {})[descriptor["token"]] = descriptor


def _remove_active_run(user_id: int, token: str) -> None:
    with _active_runs_lock:
        runs = _active_agent_runs.get(user_id)
        if runs:
            runs.pop(token, None)
            if not runs:
                _active_agent_runs.pop(user_id, None)


def _list_active_runs(user_id: int) -> list[dict]:
    """Descriptors for this user's in-flight runs, newest first."""
    with _active_runs_lock:
        runs = list(_active_agent_runs.get(user_id, {}).values())
    runs.sort(key=lambda r: r.get("started_at", ""), reverse=True)
    return runs


def _launch_agent_run(
    spec: AgentSpec, user_id: int, client_tz: str | None,
    *, agent_id: str | None = None,
) -> None:
    """Start a background-agent run for ``spec`` against ``user_id``'s data on a
    daemon thread and return immediately. The run persists its own encrypted run
    record (and converts its own failures into an ``error`` record), so there is
    nothing to await here — open panes learn the outcome via the run-update
    fanout fired when the thread finishes. Shared by the ad-hoc launcher and the
    saved-agent run button so both behave identically.

    ``agent_id`` ties the run back to a saved agent (None for ad-hoc), so the UI
    can mark that agent's card as running."""
    ctx = _context_for(user_id)
    username = ctx.username
    dek = _auth_backend.get_dek(user_id)
    runs_dir = _agent_runs_dir(user_id)
    # Outbound-messaging destination, read fresh from auth (the source of truth —
    # it may have changed since the session was built). None => the agent gets a
    # graceful "no contact connected" result instead of a misfire.
    _rec = _auth_backend.lookup(user_id)
    messaging_contact = _rec.messaging_contact if _rec else None
    # Fall back to the user's last-seen timezone when the caller didn't supply
    # one (e.g. a scheduled run on a schedule with no tz). Better than the
    # runner's server-local default, which would stamp the wrong "now" on a run
    # for a user who isn't in the server's zone.
    if not client_tz and _rec is not None:
        client_tz = _rec.tz

    token = base64.b16encode(os.urandom(8)).decode().lower()
    descriptor = {
        "token": token,
        "agent_id": agent_id,
        "agent_name": spec.name,
        "started_at": datetime.datetime.now(datetime.timezone.utc)
                          .isoformat(timespec="seconds"),
    }
    _add_active_run(user_id, descriptor)

    def _run() -> None:
        try:
            BackgroundAgentRunner().run(
                spec,
                user_id=user_id,
                dek=dek,
                runs_dir=runs_dir,
                usage_label=username,
                client_tz=client_tz,
                messaging_contact=messaging_contact,
                api_url=aime_config.API_URL,
                agent_id=agent_id,
            )
        except Exception:
            # The runner already converts its own failures into a persisted
            # error run; a failure here means it never got that far. Nothing
            # actionable to do but stop quietly — the pane refresh below still
            # fires so the UI doesn't hang on "running…".
            pass
        finally:
            _remove_active_run(user_id, token)
            ctx.notify_agent_run_update()

    threading.Thread(
        target=_run, name=f"agent-run-{user_id}", daemon=True
    ).start()
    # Tell open panes a run is now in flight (so the "running" entry appears
    # immediately); the result lands via the run record + the finished fanout.
    ctx.notify_agent_run_update()


@app.route("/agent-runs")
@login_required
def agent_runs():
    """Lightweight metadata for every stored run, newest first — enough to
    list them without pulling full transcripts into the listing. ``active`` adds
    the runs in flight right now (which have no record on disk yet) so the UI can
    show what's currently running."""
    return jsonify({
        "runs": _agent_run_store(g.user_id).list_runs(),
        "active": _list_active_runs(g.user_id),
    })


@app.route("/agent-runs/<run_id>")
@login_required
def agent_run(run_id: str):
    """A single run record: status, summary, structured result, and the full
    transcript, so the frontend can show what the agent actually did."""
    record = _agent_run_store(g.user_id).load(run_id)
    if record is None:
        return jsonify({"ok": False, "error": "run not found"}), 404
    return jsonify({"ok": True, "run": record})


@app.route("/agents/run", methods=["POST"])
@login_required
@api_access_required
def agents_run():
    """Run an ad-hoc background agent: the caller supplies a system message
    (the task brief) and we stand up a one-off, in-memory agent to carry it
    out against this user's data. The agent is registered under a unique name
    so it's a genuine registry-dispatched run, but nothing about it is
    persisted as a definition — it's gone on the next server restart. Only the
    encrypted *run record* survives (in agent_runs/), which the run viewer and
    the agents pane read back. Gated like /send because a run spends tokens."""
    data = request.get_json(silent=True) or {}
    instructions = (data.get("instructions") or "").strip()
    if not instructions:
        return jsonify({"ok": False, "error": "a system message is required"}), 400
    allow_web = bool(data.get("allow_web_search", False))
    tz = data.get("tz")
    client_tz = tz if isinstance(tz, str) and tz else None

    # Unique, registry-safe name for this one-off agent. The timestamp keeps
    # runs listed in creation order; the random suffix avoids collisions.
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    name = f"adhoc-{stamp}-{base64.b16encode(os.urandom(3)).decode().lower()}"
    # An ad-hoc run is a one-off the user typed by hand, so it gets the full data
    # toolset; web search rides the same allowlist and follows the checkbox.
    spec = AgentSpec(
        name=name,
        description="Ad-hoc agent",
        instructions=instructions,
        tool_allowlist=permissions_to_allowlist(
            modify_topics=True,
            modify_events=True,
            send_message=True,
            web_search=allow_web,
        ),
    )
    _register_agent(spec)
    _launch_agent_run(spec, g.user_id, client_tz)
    return jsonify({"ok": True, "agent_name": name})


# --- Saved agents ----------------------------------------------------------
# A per-user library of agent definitions the user created in the UI. Unlike
# ad-hoc runs (which vanish on restart), these persist as encrypted records in
# the user's agents/ directory, so an agent can be re-run, edited, or deleted —
# and, later, run on a schedule. CRUD here; the actual run reuses the same
# launcher the ad-hoc path does.


def _agent_def_public(record: dict) -> dict:
    """Project a stored definition to the fields the frontend needs. (Currently
    the whole record is safe to expose, but going through a projection keeps the
    wire shape decoupled from on-disk additions.)"""
    return {
        "agent_id": record.get("agent_id", ""),
        "name": record.get("name", ""),
        "description": record.get("description", ""),
        "instructions": record.get("instructions", ""),
        "allow_web_search": bool(record.get("allow_web_search", False)),
        "allow_modify_topics": bool(record.get("allow_modify_topics", False)),
        "allow_modify_events": bool(record.get("allow_modify_events", False)),
        "allow_send_message": bool(record.get("allow_send_message", False)),
        "created_at": record.get("created_at", ""),
        "updated_at": record.get("updated_at", ""),
    }


@app.route("/agents", methods=["GET"])
@login_required
def agents_list():
    """Every saved agent this user has defined, newest first."""
    agents = _agent_def_store(g.user_id).list_agents()
    return jsonify({"agents": [_agent_def_public(a) for a in agents]})


@app.route("/agents", methods=["POST"])
@login_required
def agents_create():
    """Create and persist a new saved agent. A name and instructions are
    required. Scheduling is set separately via /schedules."""
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    instructions = (data.get("instructions") or "").strip()
    if not name:
        return jsonify({"ok": False, "error": "a name is required"}), 400
    if not instructions:
        return jsonify({"ok": False, "error": "instructions are required"}), 400
    record = make_definition(
        name=name,
        instructions=instructions,
        description=(data.get("description") or "").strip(),
        allow_web_search=bool(data.get("allow_web_search", False)),
        allow_modify_topics=bool(data.get("allow_modify_topics", False)),
        allow_modify_events=bool(data.get("allow_modify_events", False)),
        allow_send_message=bool(data.get("allow_send_message", False)),
    )
    if not _agent_def_store(g.user_id).save(record):
        return jsonify({"ok": False, "error": "couldn't save the agent"}), 500
    return jsonify({"ok": True, "agent": _agent_def_public(record)})


@app.route("/agents/<agent_id>", methods=["PUT"])
@login_required
def agents_update(agent_id: str):
    """Update an existing saved agent. Only the fields present in the request
    are changed; the id and creation time are preserved."""
    store = _agent_def_store(g.user_id)
    record = store.load(agent_id)
    if record is None:
        return jsonify({"ok": False, "error": "agent not found"}), 404
    data = request.get_json(silent=True) or {}
    if "name" in data:
        name = (data.get("name") or "").strip()
        if not name:
            return jsonify({"ok": False, "error": "a name is required"}), 400
        record["name"] = name
    if "instructions" in data:
        instructions = (data.get("instructions") or "").strip()
        if not instructions:
            return jsonify({"ok": False, "error": "instructions are required"}), 400
        record["instructions"] = instructions
    if "description" in data:
        record["description"] = (data.get("description") or "").strip()
    if "allow_web_search" in data:
        record["allow_web_search"] = bool(data.get("allow_web_search"))
    if "allow_modify_topics" in data:
        record["allow_modify_topics"] = bool(data.get("allow_modify_topics"))
    if "allow_modify_events" in data:
        record["allow_modify_events"] = bool(data.get("allow_modify_events"))
    if "allow_send_message" in data:
        record["allow_send_message"] = bool(data.get("allow_send_message"))
    if not store.save(record):
        return jsonify({"ok": False, "error": "couldn't save the agent"}), 500
    return jsonify({"ok": True, "agent": _agent_def_public(record)})


@app.route("/agents/<agent_id>", methods=["DELETE"])
@login_required
def agents_delete(agent_id: str):
    """Delete a saved agent definition. The run records it already produced are
    left in place — they're a separate, immutable audit trail."""
    _agent_def_store(g.user_id).delete(agent_id)
    return jsonify({"ok": True})


@app.route("/agents/<agent_id>/run", methods=["POST"])
@login_required
@api_access_required
def agents_run_saved(agent_id: str):
    """Run a saved agent now. Builds the spec from the stored definition and
    hands it to the same launcher the ad-hoc path uses. Gated like /send
    because a run spends tokens."""
    record = _agent_def_store(g.user_id).load(agent_id)
    if record is None:
        return jsonify({"ok": False, "error": "agent not found"}), 404
    data = request.get_json(silent=True) or {}
    tz = data.get("tz")
    client_tz = tz if isinstance(tz, str) and tz else None
    spec = definition_to_spec(record)
    _launch_agent_run(spec, g.user_id, client_tz, agent_id=agent_id)
    return jsonify({"ok": True, "agent_name": spec.name})


# --- Scheduled things ------------------------------------------------------
# A per-user library of schedule records (scheduled agents + event reminders),
# each pairing a trigger (when) with an action (what). The background loop fires
# them; these routes are just CRUD over the encrypted store. See
# docs/scheduling.md for the record shape and invariants.


def _schedule_public(record: dict) -> dict:
    """Project a stored schedule to the wire shape. The record is the user's own
    data, so this is mostly pass-through; going through a projection keeps the
    API decoupled from on-disk additions and surfaces ``state`` (last run /
    sent markers) the UI uses for 'next run' hints."""
    return {
        "schedule_id": record.get("schedule_id", ""),
        "enabled": bool(record.get("enabled", True)),
        "tz": record.get("tz", ""),
        "trigger": record.get("trigger", {}),
        "action": record.get("action", {}),
        "state": record.get("state", {}),
        "created_at": record.get("created_at", ""),
        "updated_at": record.get("updated_at", ""),
    }


@app.route("/schedules", methods=["GET"])
@login_required
def schedules_list():
    """Every schedule this user has, newest first."""
    items = _schedule_store(g.user_id).list_schedules()
    return jsonify({"schedules": [_schedule_public(s) for s in items]})


@app.route("/schedules", methods=["POST"])
@login_required
def schedules_create():
    """Create a schedule from a ``{trigger, action, tz, enabled?, label?}`` body.
    Validated against the tagged-variant invariants; a bad record is a 400 with
    the precise reason."""
    data = request.get_json(silent=True) or {}
    record = make_schedule(
        trigger=data.get("trigger") or {},
        action=data.get("action") or {},
        tz=(data.get("tz") or "").strip(),
        enabled=bool(data.get("enabled", True)),
        label=(data.get("label") or "").strip(),
    )
    err = validate_schedule(record)
    if err is not None:
        return jsonify({"ok": False, "error": err}), 400
    if not _schedule_store(g.user_id).save(record):
        return jsonify({"ok": False, "error": "couldn't save the schedule"}), 500
    return jsonify({"ok": True, "schedule": _schedule_public(record)})


@app.route("/schedules/<schedule_id>", methods=["PUT"])
@login_required
def schedules_update(schedule_id: str):
    """Update an existing schedule. Only the fields present in the request are
    changed; ``state``, id, and creation time are preserved. Editing the trigger
    clears stale fire-state so the change takes effect cleanly."""
    store = _schedule_store(g.user_id)
    record = store.load(schedule_id)
    if record is None:
        return jsonify({"ok": False, "error": "schedule not found"}), 404
    data = request.get_json(silent=True) or {}
    if "enabled" in data:
        record["enabled"] = bool(data.get("enabled"))
    if "tz" in data:
        record["tz"] = (data.get("tz") or "").strip()
    if "trigger" in data:
        record["trigger"] = data.get("trigger") or {}
        # The trigger defines what the fire-state tracks; a changed trigger makes
        # the old markers meaningless, so reset them to re-arm from scratch.
        record["state"] = {"last_run_at": None, "fired_at": None, "sent_for_start": None}
    if "action" in data:
        record["action"] = data.get("action") or {}
    err = validate_schedule(record)
    if err is not None:
        return jsonify({"ok": False, "error": err}), 400
    if not store.save(record):
        return jsonify({"ok": False, "error": "couldn't save the schedule"}), 500
    return jsonify({"ok": True, "schedule": _schedule_public(record)})


@app.route("/schedules/<schedule_id>", methods=["DELETE"])
@login_required
def schedules_delete(schedule_id: str):
    """Delete a schedule. Any agent runs it already produced are left in place."""
    _schedule_store(g.user_id).delete(schedule_id)
    return jsonify({"ok": True})


@app.route("/schedules/<schedule_id>/run", methods=["POST"])
@login_required
@api_access_required
def schedules_run_now(schedule_id: str):
    """Fire a schedule's action immediately — a manual test trigger. Doesn't
    touch fire-state, so the normal cadence is unaffected."""
    record = _schedule_store(g.user_id).load(schedule_id)
    if record is None:
        return jsonify({"ok": False, "error": "schedule not found"}), 404
    data = request.get_json(silent=True) or {}
    tz = data.get("tz")
    client_tz = tz if isinstance(tz, str) and tz else record.get("tz")
    _dispatch_schedule_now(g.user_id, record, client_tz)
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
    # The replace_event call flows through the gateway's on_mutation choke point,
    # which marks this record stale for the model (and refreshes the UI) — the
    # same path topic edits use. No explicit stale call needed here.
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


# ---------------------------------------------------------------------------
# Topic sharing
#
# A topic lives only in its owner's silo. Sharing grants server-mediated
# access: a recipient addresses a shared topic by the composite handle
# "<owner_id>:<topic_id>", and the server fetches it through the *owner's*
# gateway after checking _share_store for a matching accepted grant. The
# recipient never gets the owner's key; revocation is just deleting the grant.
# See aime.topic_shares for the data model.
# ---------------------------------------------------------------------------


class _ShareAccessError(Exception):
    """Raised by the topic resolver when a request may not touch a topic.
    Carries an HTTP status + a user-facing message the routes turn into JSON."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def _parse_topic_handle(handle: str) -> tuple[int | None, int]:
    """Split a topic handle into (owner_id, topic_id). Own topics are bare
    integers and return owner_id=None; shared topics are "<owner_id>:<topic_id>".
    Raises _ShareAccessError(400) on anything malformed."""
    if ":" in handle:
        owner_s, _, tid_s = handle.partition(":")
        if owner_s.isdigit() and tid_s.isdigit():
            return int(owner_s), int(tid_s)
        raise _ShareAccessError(400, "invalid topic id")
    if handle.isdigit():
        return None, int(handle)
    raise _ShareAccessError(400, "invalid topic id")


def _resolve_topic_as(user_id: int, handle: str, need: str) -> tuple[int, int]:
    """Resolve a topic handle *as `user_id`* to (effective_owner_id, topic_id),
    enforcing access. `need` is "view" or "edit". The g-free core of
    :func:`_resolve_topic`, so it can run off the request thread — e.g. from the
    graphic-store provider a model turn calls on a worker thread.

    For the user's own topics this is the identity mapping. For a shared topic
    it verifies an *accepted* grant exists (and that it carries edit rights when
    need=="edit") before returning the owner's id as the effective backend user.
    The owner id is taken from _share_store, never trusted from the client, so a
    forged handle can't reach a topic that wasn't actually shared."""
    owner_id, topic_id = _parse_topic_handle(handle)
    if owner_id is None or owner_id == user_id:
        return user_id, topic_id
    share = _share_store.get(owner_id, topic_id, user_id)
    if share is None or share.status != _topic_shares.STATUS_ACCEPTED:
        raise _ShareAccessError(403, "this topic isn't shared with you")
    if need == "edit" and share.permission != _topic_shares.PERM_EDIT:
        raise _ShareAccessError(403, "you have view-only access to this topic")
    return owner_id, topic_id


def _resolve_topic(handle: str, need: str) -> tuple[int, int]:
    """Resolve a topic handle from the *current request's* user. Thin wrapper
    over :func:`_resolve_topic_as` bound to ``g.user_id``."""
    return _resolve_topic_as(g.user_id, handle, need)


def _foreign_graphic_tag(contents: str, owner_id: int,
                         topic_id: int) -> str | None:
    """The write rule (docs/graphics-sharing.md §3b) for a topic-body save:
    return the handle of the first `[graphic-…]` tag that doesn't belong to this
    topic, or None if every tag is in-scope. A tag is judged exactly as it
    renders — a bare `graphic-T:n` belongs to the topic's owner, an explicit
    `graphic-O:T:n` names its owner — and must denote this same ``(owner,
    topic)``. A personal graphic or another topic's graphic is rejected; a
    recipient can save a shared body that still carries the owner's bare tags.
    Mirrors the model-side rule in the controller."""
    for handle in _graphics.graphic_tag_handles(contents):
        if tag_handle_scope(handle, owner_id) != (owner_id, topic_id):
            return handle
    return None


def _make_graphic_store_provider(user_id: int):
    """Build the graphic-store provider for `user_id`: a closure that maps a
    topic handle ("0" personal, "T" own, "O:T" shared) to the scoped GraphicStore
    backing it, or None if `user_id` may not write/read that target.

    It is just :func:`_resolve_topic_as` plus a store: the resolve enforces the
    grant (and edit right when asked), then the store is opened in the *owner's*
    silo under the owner's DEK — the server writing on the recipient's behalf,
    exactly as a shared topic's *text* edits already land. Captures `user_id` so
    it is safe to call off the request thread (the controller runs on a worker).
    Handed to the controller, which never reaches across the trust boundary
    itself."""
    def provider(handle: str, need: str = "edit"):
        try:
            owner_id, topic_id = _resolve_topic_as(user_id, handle, need)
        except _ShareAccessError:
            return None
        return GraphicStore(
            _graphics_dir(owner_id), _auth_backend.get_dek(owner_id),
            owner_id, topic_id)
    return provider


def _user_owns_topic(user_id: int, topic_id: int) -> bool:
    """True if `topic_id` is one of `user_id`'s own topics. A light list scan —
    topics number in the tens — used to gate the owner-only share endpoints."""
    try:
        for t in _context_for(user_id).topic_service.list_topics():
            if int(t.get("id", -1)) == topic_id:
                return True
    except Exception:
        pass
    return False


def _shared_topics_for(recipient_id: int) -> list[dict]:
    """Topic-list entries for everything shared with `recipient_id` (accepted =
    openable, pending = greyed, awaiting a response). Borrows title/category
    from each owner's store for display; the content itself is never copied.
    Grants whose topic the owner has since deleted are skipped."""
    shares = _share_store.incoming(
        recipient_id,
        statuses=(_topic_shares.STATUS_ACCEPTED, _topic_shares.STATUS_PENDING),
    )
    if not shares:
        return []
    by_owner: dict[int, list] = {}
    for s in shares:
        by_owner.setdefault(s.owner_id, []).append(s)
    out: list[dict] = []
    for owner_id, owner_shares in by_owner.items():
        owner_rec = _auth_backend.lookup(owner_id)
        owner_name = owner_rec.username if owner_rec else "(unknown)"
        try:
            owner_topics = {
                int(t.get("id", -1)): t
                for t in _context_for(owner_id).topic_service.list_topics()
            }
        except Exception:
            owner_topics = {}
        for s in owner_shares:
            meta = owner_topics.get(s.topic_id)
            if meta is None:
                continue
            entry = {
                "id": f"{owner_id}:{s.topic_id}",
                "title": meta.get("title") or meta.get("name") or "(untitled)",
                "category": meta.get("category") or "",
                "summary": meta.get("summary") or "",
                "folder": meta.get("folder") or "",
                "shared": True,
                "permission": s.permission,
                "status": s.status,
                "owner": owner_name,
            }
            # Current edit-lock holder (if any) so the recipient's Edit button
            # starts in the right state; topic_lock events keep it live after.
            if s.status == _topic_shares.STATUS_ACCEPTED:
                entry["locked_by"] = _username_of(
                    _edit_locks.holder(owner_id, s.topic_id)
                )
            out.append(entry)
    return out


class _RecordSyncBridge:
    """Adapter handed to a user's ConversationController for cross-user record
    access — currently shared topics, with room for more shared kinds.

    The controller is deliberately ignorant of sharing (it's reused by the TUI
    and background agents); this bridge holds the cross-user knowledge —
    `_share_store` for grants and `_context_for` to reach an owner's silo — and
    is bound to one ``user_id`` for the life of that user's context. The entry
    points the controller calls:

    * :meth:`run_if_shared` reroutes a per-topic tool addressed to a composite
      ``"<owner>:<topic>"`` handle into the owner's gateway after checking the
      grant, so the model can read (and, with edit rights, write) a topic that
      physically lives in someone else's silo. Returns ``None`` for a bare id so
      the controller runs it normally against this user's own gateway.
    * :meth:`merge_shared_into_list` enriches a FilterTopics result with the
      topics shared *with* this user and flags which of their *own* topics are
      shared out (and to whom), so the model knows the state on both sides.
    * :meth:`after_model_write` clears this user's own stale flag once its model
      has written a tracked record (the choke point flags every party; the actor
      already has the fresh content).

    Routing writes through the owner's gateway is deliberate: it lands the edit
    in the owner's silo and trips that gateway's on_mutation choke point, so the
    change syncs back to the owner (and every share partner) exactly like a UI
    edit — the same single path every record change flows through.
    """

    # Of the shareable tools, these mutate and so require edit permission;
    # GetTopicContents needs only an accepted (view) grant.
    _WRITE_TOOLS = frozenset({"ReplaceTopicContents", "EditTopicContents"})

    def __init__(self, user_id: int):
        self.user_id = user_id

    @staticmethod
    def _parse_handle(value) -> tuple[int, int] | None:
        """``(owner_id, topic_id)`` if `value` is a composite
        ``"<owner>:<topic>"`` handle, else ``None`` (a bare id is one of this
        user's own topics and takes the normal path)."""
        if isinstance(value, str) and ":" in value:
            owner_s, _, tid_s = value.partition(":")
            if owner_s.isdigit() and tid_s.isdigit():
                return int(owner_s), int(tid_s)
        return None

    def run_if_shared(self, agent_tool_name: str, tool_input: dict):
        """Run a per-topic tool against the owner's silo when its id is a shared
        handle; otherwise return ``None`` so the caller runs it locally.

        Enforces the grant exactly as the topic routes do: an accepted grant is
        required to read, and edit permission to write. A denied or unknown
        grant comes back as a friendly ``{"error": ...}`` the model can relay,
        never an exception that breaks the turn."""
        raw_id = (tool_input or {}).get("id")
        parsed = self._parse_handle(raw_id)
        if parsed is None:
            # A malformed composite handle ("2:foo", "x:y") still carries a ":",
            # marking the model's intent to reach a shared topic. Refuse it here
            # rather than returning None, which would let it fall through to the
            # local gateway and be truncated to a bare own-topic id.
            if isinstance(raw_id, str) and ":" in raw_id:
                return {"error": "this topic isn't shared with you"}
            return None
        owner_id, topic_id = parsed
        share = _share_store.get(owner_id, topic_id, self.user_id)
        if share is None or share.status != _topic_shares.STATUS_ACCEPTED:
            return {"error": "this topic isn't shared with you"}
        if (agent_tool_name in self._WRITE_TOOLS
                and share.permission != _topic_shares.PERM_EDIT):
            return {"error": "you have view-only access to this shared topic"}
        payload = dict(tool_input or {})
        payload["id"] = topic_id
        try:
            return _context_for(owner_id).gateway.execute(agent_tool_name, payload)
        except Exception as exc:  # noqa: BLE001
            return {"error": f"shared topic access failed: {exc}"}

    def after_model_write(self, agent_tool_name: str, tool_input: dict, result) -> None:
        """Clear this user's own stale flag for a record its model just wrote.

        The mutation choke point flags a changed record as stale for every party
        who can see it, this user included. When the change came from this user's
        *own* model it already holds the fresh content, so that flag would only
        force a needless re-read — drop it. The id is taken straight from the
        tool input, which is already in this user's own addressing form (a bare
        id for an own record, the ``"<owner>:<topic>"`` handle for a shared one),
        so it matches what the choke point recorded for this context. A no-op for
        reads, failed writes, and tools that don't change a tracked record."""
        if isinstance(result, dict) and "error" in result:
            return
        backend_tool = TOOL_NAME_MAP.get(agent_tool_name, agent_tool_name)
        kind = _RECORD_KIND_BY_TOOL.get(backend_tool)
        if kind is None:
            return
        record_id = (tool_input or {}).get("id")
        if record_id is None:
            return
        ctx = _user_contexts.get(self.user_id)
        if ctx is not None:
            try:
                ctx.clear_record_stale(kind, record_id)
            except Exception:
                pass

    def merge_shared_into_list(self, result, tool_input: dict | None = None):
        """Append the topics shared *with* this user to a FilterTopics result
        and tag the user's own shared-out topics with their recipients. Wholly
        best-effort — any hiccup returns the original result so the model still
        gets its own topics.

        `tool_input` carries the FilterTopics filters (category / categories /
        keyword). The backend already applied them to the owned topics; we apply
        the same ones to the shared additions so a narrowed query stays coherent
        rather than dumping every shared topic in regardless. `limit` is left to
        the backend's own slice — shared topics are few and hiding them would
        defeat the point, so we don't re-trim the merged list."""
        try:
            if isinstance(result, dict):
                if "error" in result:
                    return result
                owned = list(result.get("topics") or [])
            elif isinstance(result, list):
                owned = list(result)
            else:
                return result
            self._annotate_owned(owned)
            shared = [
                e for e in self._shared_with_me()
                if self._matches_filters(e, tool_input or {})
            ]
            merged = owned + shared
        except Exception:
            return result
        if isinstance(result, dict):
            out = dict(result)
            out["topics"] = merged
            if "count" in out:
                out["count"] = len(merged)
            return out
        return merged

    @staticmethod
    def _matches_filters(entry: dict, filters: dict) -> bool:
        """Apply the FilterTopics category/keyword filters to a shared entry,
        mirroring the backend's semantics: a single `category` is an exact match
        and overrides `categories` (an OR set); `keyword` is a case-insensitive
        substring over title + summary. Absent filters match everything."""
        cat = (filters.get("category") or "").strip()
        if cat:
            if (entry.get("category") or "") != cat:
                return False
        else:
            cats = filters.get("categories")
            if isinstance(cats, list) and cats:
                if (entry.get("category") or "") not in cats:
                    return False
        kw = (filters.get("keyword") or "").strip().lower()
        if kw:
            hay = (
                (entry.get("title") or "") + " " + (entry.get("summary") or "")
            ).lower()
            if kw not in hay:
                return False
        return True

    def _annotate_owned(self, topics: list) -> None:
        """Tag each of this user's own topics that is shared out with the list
        of accepted recipient usernames, so the model can say who can see it."""
        shared_ids = _share_store.owner_shared_topic_ids(self.user_id)
        if not shared_ids:
            return
        for t in topics:
            if not isinstance(t, dict):
                continue
            try:
                tid = int(t.get("id", -1))
            except (TypeError, ValueError):
                continue
            if tid in shared_ids:
                names = [
                    _username_of(uid)
                    for uid in _share_store.topic_partners(self.user_id, tid)
                ]
                t["shared_with"] = [n for n in names if n]

    def _shared_with_me(self) -> list[dict]:
        """Topic-list entries for topics this user has *accepted* from others —
        the ones the model can actually open via their composite id. (Pending
        offers are intentionally excluded; the model can't read those yet.)"""
        return [
            e for e in _shared_topics_for(self.user_id)
            if e.get("status") == _topic_shares.STATUS_ACCEPTED
        ]


def _notify_share(user_id: int, message_text: str) -> None:
    """Tell `user_id` about a sharing change: refresh their open sessions
    (in-app, via SSE) and, if they've connected one, text them (out-of-app, via
    the messaging layer). Best-effort — a notification failure never fails the
    underlying action."""
    ctx = _user_contexts.get(user_id)
    if ctx is not None:
        try:
            ctx.notify_share_update()
        except Exception:
            pass
    try:
        rec = _auth_backend.lookup(user_id)
        if rec and rec.messaging_contact:
            from aime import messaging as _aime_messaging
            messenger = _aime_messaging.get_messenger()
            if messenger is not None:
                messenger.send(rec.messaging_contact, message_text)
    except Exception:
        pass


# --- Unified record-change propagation -------------------------------------
#
# A single notification path for "a tracked record changed", driven from the
# gateway's on_mutation choke point so every write (UI or model, own or shared)
# flows through it automatically — add a new shared record type by registering
# its mutation tools below and teaching `_record_partners`/`_record_title` about
# it, not by wiring a fresh fan-out at each call site.

# Backend mutation tool name -> the record kind it changes. Only tools listed
# here trigger a stale/refresh fan-out; everything else still gets the plain
# own-UI ping. (Creates are intentionally absent: a brand-new record has no
# prior view to invalidate.)
_RECORD_KIND_BY_TOOL = {
    "replace_topic_contents": "topic",
    "edit_topic_contents": "topic",
    "replace_event": "event",
}


def _record_partners(kind: str, owner_id: int, record_id: int) -> list[int]:
    """Accepted recipients (not incl. the owner) who can see a shared record.
    Empty for kinds that aren't shareable yet — events have no recipients, so a
    change to one only ever notifies its owner."""
    if kind == "topic":
        return _share_store.topic_partners(owner_id, record_id)
    return []


def _record_title(kind: str, owner_id: int, record_id: int, payload: dict) -> str:
    """A human-readable title for the <stale> tag, best-effort. Event writes
    carry the title in their payload (no re-read needed); topic-content writes
    don't, so fall back to a quick lookup in the owner's topic list."""
    if kind == "event":
        title = payload.get("title") if isinstance(payload, dict) else ""
        return (title or "").strip()
    if kind == "topic":
        return _topic_title(owner_id, record_id)
    return ""


def _propagate_record_change(
    kind: str, owner_id: int, record_id: int, payload: dict,
) -> None:
    """Fan a record change out to everyone who can see it — the owner plus any
    accepted recipients. Each party's model gets a <stale> tag on its next turn
    (with the id in the form *they* address the record by: the owner the bare
    id, a recipient the "<owner>:<record>" handle) and each recipient's live UI
    is refreshed. The owner's own UI is pinged separately by the caller, so it's
    only marked stale here. Only parties with a live context are touched — an
    inactive user rebuilds fresh state next time they interact. Best-effort.

    When the change came from a party's *own* model, that model already holds
    the fresh content; it clears its own flag right after (see
    `_RecordSyncBridge.after_model_write`), so this can mark unconditionally."""
    title = _record_title(kind, owner_id, record_id, payload)
    owner_ctx = _user_contexts.get(owner_id)
    if owner_ctx is not None:
        try:
            owner_ctx.mark_record_stale(kind, record_id, title)
        except Exception:
            pass
    for uid in _record_partners(kind, owner_id, record_id):
        ctx = _user_contexts.get(uid)
        if ctx is None:
            continue
        try:
            ctx.mark_record_stale(kind, f"{owner_id}:{record_id}", title)
            ctx.notify_remote_edit(f"{kind}:{owner_id}")
        except Exception:
            pass


def _username_of(user_id: int | None) -> str | None:
    if user_id is None:
        return None
    rec = _auth_backend.lookup(user_id)
    return rec.username if rec else None


def _broadcast_topic_lock(
    owner_id: int, topic_id: int, locked: bool, locked_by: str | None,
    *, exclude: int | None = None,
) -> None:
    """Tell everyone who can see a shared topic that its edit-lock state
    changed, so their Edit affordance updates live. Targets the owner plus the
    topic's accepted recipients; `exclude` skips the actor (their own UI already
    reflects the change)."""
    payload = {
        "kind": "topic_lock",
        "owner_id": owner_id,
        "topic_id": topic_id,
        "locked": locked,
        "locked_by": locked_by,
    }
    targets = [owner_id] + _share_store.topic_partners(owner_id, topic_id)
    for uid in targets:
        if uid == exclude:
            continue
        ctx = _user_contexts.get(uid)
        if ctx is not None:
            try:
                ctx.notify_topic_lock(payload)
            except Exception:
                pass


@app.route("/topics")
@login_required
def topics():
    try:
        items = _context_for(g.user_id).topic_service.list_topics()
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500
    # Flag the owner's own topics that are shared with someone (so the client
    # engages the edit-lock on them) and surface any current lock holder.
    try:
        shared_ids = _share_store.owner_shared_topic_ids(g.user_id)
        for it in items:
            tid = int(it.get("id", -1))
            if tid in shared_ids:
                it["shared_with_others"] = True
                holder = _edit_locks.holder(g.user_id, tid)
                if holder is not None and holder != g.user_id:
                    it["locked_by"] = _username_of(holder)
    except Exception:
        pass
    # Merge in topics shared with this user. Wrapped so a sharing hiccup can
    # never take down the user's own topic list.
    try:
        items = items + _shared_topics_for(g.user_id)
    except Exception:
        pass
    return jsonify({"topics": items})


@app.route("/graphics/<graphic_id>")
@login_required
def graphic_asset(graphic_id: str):
    """Return one stored graphic asset — the source a `[graphic-<handle>:N]` tag
    (placed in a topic body, or rendered in chat) resolves to.

    The id *is* a topic handle plus an ordinal, so authorization is topic
    authorization: parse it, resolve the handle with `_resolve_topic("view")`
    (own topic ⇒ self; shared ⇒ an accepted grant; personal `0` ⇒ self only),
    then open the (owner, topic) store under the owner's DEK. A legacy bare
    `graphic-N` reads as personal `graphic-0:N`. Anything that doesn't
    resolve — bad id, no grant, missing file — collapses to 404, so a stale tag
    degrades to a calm 'couldn't load' card and existence is never leaked."""
    parsed = parse_graphic_id(graphic_id)
    if parsed is None:
        return jsonify({"ok": False, "error": "not found"}), 404
    handle, n = parsed
    try:
        owner_id, topic_id = _resolve_topic(handle, "view")
    except _ShareAccessError:
        return jsonify({"ok": False, "error": "not found"}), 404
    try:
        store = GraphicStore(
            _graphics_dir(owner_id), _auth_backend.get_dek(owner_id),
            owner_id, topic_id)
        record = store.load(make_graphic_id(n))
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500
    if not record:
        return jsonify({"ok": False, "error": "not found"}), 404
    return jsonify({
        "format": record.get("format") or "",
        "source": record.get("source") or "",
        "summary": record.get("summary") or "",
    })


@app.route("/topics/<topic_id>")
@login_required
def topic_contents(topic_id: str):
    try:
        owner_id, tid = _resolve_topic(topic_id, "view")
    except _ShareAccessError as e:
        return jsonify({"ok": False, "error": e.message}), e.status
    try:
        contents = _context_for(owner_id).topic_service.get_topic_contents(tid)
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


@app.route("/topics/<topic_id>/export", methods=["GET", "POST"])
@login_required
def topic_export(topic_id: str):
    try:
        owner_id, tid = _resolve_topic(topic_id, "view")
    except _ShareAccessError as e:
        return jsonify({"ok": False, "error": e.message}), e.status
    fmt = (request.args.get("format") or "md").lower()
    if fmt not in _EXPORT_FORMATS:
        return jsonify({"ok": False, "error": "unsupported format"}), 400
    target, ext, mime = _EXPORT_FORMATS[fmt]
    ctx = _context_for(owner_id)
    # The client may POST a rendered copy of the body to embed in the document —
    # e.g. with each [graphic-…] tag swapped for an inline PNG data URI, which
    # only the browser can rasterize (mermaid/vega need a DOM). When absent we
    # export the stored body verbatim. Either way the reader must have view
    # access (checked above), and the title always comes from metadata.
    override_md = None
    if request.method == "POST":
        body = request.get_json(silent=True) or {}
        if isinstance(body.get("markdown"), str):
            override_md = body["markdown"]
    try:
        markdown = (override_md if override_md is not None
                    else ctx.topic_service.get_topic_contents(tid))
        # Title comes from the topics list — get_topic_contents only returns
        # the body, not metadata. A small list scan is fine (topics are tens,
        # not thousands) and keeps this route independent of how the gateway
        # exposes single-topic metadata.
        title = ""
        for t in ctx.topic_service.list_topics():
            if int(t.get("id", -1)) == tid:
                title = t.get("title") or t.get("name") or ""
                break
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500

    filename = f"{_safe_filename(title, f'topic-{tid}')}.{ext}"
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
    try:
        owner_id, tid = _resolve_topic(topic_id, "edit")
    except _ShareAccessError as e:
        return jsonify({"ok": False, "error": e.message}), e.status
    data = request.get_json(silent=True) or {}
    contents = data.get("contents")
    if not isinstance(contents, str):
        return jsonify({"ok": False, "error": "contents (string) required"}), 400
    # Hard cap on topic body size — prevents a malicious or runaway client
    # from filling the disk via repeated PUTs.
    if len(contents.encode("utf-8")) > 2 * 1024 * 1024:
        return jsonify({"ok": False, "error": "contents too large (max 2 MiB)"}), 413
    # Write rule: a topic body may embed only its own graphics. Reject any
    # [graphic-…] tag that doesn't denote this topic (bare tags belong to the
    # topic's owner, so a recipient may keep the owner's bare tags).
    bad = _foreign_graphic_tag(contents, owner_id, tid)
    if bad is not None:
        return jsonify(
            {"ok": False, "error": _graphics.foreign_graphic_tag_message(bad)}), 400
    # Edits always land in the owner's silo (owner_id == g.user_id for own
    # topics; the topic owner for a shared one).
    ctx = _context_for(owner_id)
    try:
        ctx.topic_service.replace_topic_contents(tid, contents)
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500
    # No explicit notification here: the replace_topic_contents call above flows
    # through the gateway's on_mutation choke point, which fans the <stale> tag
    # and live-UI refresh out to every party — the owner and all recipients.
    # A UI edit doesn't go through the model dispatch, so nothing clears the
    # editor's own flag: their model is correctly told its view is now stale.
    return jsonify({"ok": True})


def _topic_title(user_id: int, topic_id: int) -> str:
    """Best-effort title for one of `user_id`'s topics, for notification text."""
    try:
        for t in _context_for(user_id).topic_service.list_topics():
            if int(t.get("id", -1)) == topic_id:
                return (t.get("title") or t.get("name") or "").strip()
    except Exception:
        pass
    return ""


def _share_view(share: _topic_shares.Share) -> dict:
    """Serialize a grant for the owner's "shared with" list, resolving the
    recipient's id to a username for display."""
    rec = _auth_backend.lookup(share.recipient_id)
    return {
        "recipient_id": share.recipient_id,
        "username": rec.username if rec else "(unknown)",
        "permission": share.permission,
        "status": share.status,
    }


@app.route("/topics/<int:topic_id>/share", methods=["POST"])
@login_required
def topic_share(topic_id: int):
    """Owner shares one of their own topics with another user by username.
    Body: {username, permission?: "view"|"edit"}. Idempotent — re-sharing
    updates the permission (and re-offers a previously declined grant)."""
    if not _user_owns_topic(g.user_id, topic_id):
        return jsonify({"ok": False, "error": "no such topic"}), 404
    data = request.get_json(silent=True) or {}
    username = (data.get("username") or "").strip()
    permission = (data.get("permission") or _topic_shares.PERM_VIEW).strip()
    if not username:
        return jsonify({"ok": False, "error": "username required"}), 400
    recipient = _auth_backend.lookup_by_username(username)
    if recipient is None:
        return jsonify({"ok": False,
                        "error": f"no user named {username!r}"}), 404
    if recipient.id == g.user_id:
        return jsonify({"ok": False,
                        "error": "you can't share a topic with yourself"}), 400
    # Distinguish a fresh (or re-sent) invite from a mere permission change on an
    # existing grant: a new invite warrants an out-of-app message; a permission
    # tweak only needs the recipient's open view to re-gate, so we keep it to a
    # quiet in-app refresh and don't re-text them.
    prior = _share_store.get(g.user_id, topic_id, recipient.id)
    is_new_invite = prior is None or prior.status == _topic_shares.STATUS_DECLINED
    try:
        share = _share_store.share(g.user_id, topic_id, recipient.id, permission)
    except _topic_shares.InvalidPermission as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    if is_new_invite:
        title = _topic_title(g.user_id, topic_id) or "a topic"
        _notify_share(
            recipient.id,
            f"{g.username} shared the topic “{title}” with you on Aime.",
        )
    else:
        # In-app refresh only — flips the recipient's access (e.g. view→edit
        # enables their Edit button) without a notification.
        ctx = _user_contexts.get(recipient.id)
        if ctx is not None:
            try:
                ctx.notify_share_update()
            except Exception:
                pass
    return jsonify({"ok": True, "share": _share_view(share)})


@app.route("/topics/<int:topic_id>/shares", methods=["GET"])
@login_required
def topic_shares_list(topic_id: int):
    """Owner-only: who this topic is shared with, and at what permission."""
    if not _user_owns_topic(g.user_id, topic_id):
        return jsonify({"ok": False, "error": "no such topic"}), 404
    shares = _share_store.for_topic(g.user_id, topic_id)
    return jsonify({"ok": True, "shares": [_share_view(s) for s in shares]})


@app.route("/topics/<int:topic_id>/unshare", methods=["POST"])
@login_required
def topic_unshare(topic_id: int):
    """Owner revokes a grant. Body: {username}. Access stops immediately."""
    if not _user_owns_topic(g.user_id, topic_id):
        return jsonify({"ok": False, "error": "no such topic"}), 404
    data = request.get_json(silent=True) or {}
    username = (data.get("username") or "").strip()
    recipient = _auth_backend.lookup_by_username(username) if username else None
    if recipient is None:
        return jsonify({"ok": False, "error": "no such user"}), 404
    removed = _share_store.revoke(g.user_id, topic_id, recipient.id)
    if removed:
        # Refresh the (ex-)recipient's sessions so the topic drops out of their
        # list. No message — quietly losing access is gentler than a "revoked"
        # notification.
        ctx = _user_contexts.get(recipient.id)
        if ctx is not None:
            try:
                ctx.notify_share_update()
            except Exception:
                pass
    return jsonify({"ok": True, "removed": removed})


@app.route("/shares/<int:owner_id>/<int:topic_id>/respond", methods=["POST"])
@login_required
def share_respond(owner_id: int, topic_id: int):
    """Recipient accepts or declines a pending grant. Body: {accept: bool}."""
    data = request.get_json(silent=True) or {}
    accept = bool(data.get("accept"))
    ok = _share_store.respond(owner_id, topic_id, g.user_id, accept)
    if not ok:
        return jsonify({"ok": False,
                        "error": "no pending share to respond to"}), 404
    # Let the owner know the outcome, and refresh the recipient's own sessions
    # so the item flips from pending to open (or disappears on decline).
    title = _topic_title(owner_id, topic_id) or "a topic"
    verb = "accepted" if accept else "declined"
    _notify_share(owner_id,
                  f"{g.username} {verb} your shared topic “{title}”.")
    ctx = _user_contexts.get(g.user_id)
    if ctx is not None:
        try:
            ctx.notify_share_update()
        except Exception:
            pass
    return jsonify({"ok": True, "accepted": accept})


@app.route("/topics/<topic_id>/lock", methods=["POST"])
@login_required
def topic_lock(topic_id: str):
    """Acquire (or refresh, via heartbeat) the advisory edit-lock on a topic
    before entering edit mode. Requires edit rights. On success broadcasts the
    lock to other viewers so their Edit button disables. 409 if someone else
    already holds it — the response names the holder."""
    try:
        owner_id, tid = _resolve_topic(topic_id, "edit")
    except _ShareAccessError as e:
        return jsonify({"ok": False, "error": e.message}), e.status
    ok, holder_id = _edit_locks.acquire(owner_id, tid, g.user_id)
    if not ok:
        return jsonify({"ok": False, "locked_by": _username_of(holder_id),
                        "is_me": False}), 409
    _broadcast_topic_lock(owner_id, tid, True, g.username, exclude=g.user_id)
    return jsonify({"ok": True, "locked_by": g.username, "is_me": True})


@app.route("/topics/<topic_id>/unlock", methods=["POST"])
@login_required
def topic_unlock(topic_id: str):
    """Release the edit-lock (on save, cancel, or page unload). Only the holder
    can release; view access is enough so a since-downgraded editor can still
    let go. Broadcasts the release so others' Edit buttons re-enable."""
    try:
        owner_id, tid = _resolve_topic(topic_id, "view")
    except _ShareAccessError as e:
        return jsonify({"ok": False, "error": e.message}), e.status
    released = _edit_locks.release(owner_id, tid, g.user_id)
    if released:
        _broadcast_topic_lock(owner_id, tid, False, None, exclude=g.user_id)
    return jsonify({"ok": True, "released": released})


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


def _serve_production(host: str, port: int) -> None:
    """Serve the app with waitress — a production-grade WSGI server — instead
    of Werkzeug's development server.

    waitress is deliberately single-process and multi-threaded, which is what
    this deployment needs: the background scheduler and the in-memory rate
    limiters both assume exactly one process (a multi-worker server like
    gunicorn -w N would double-fire the scheduler and split the limiters). It
    has no built-in TLS, so this path expects to sit behind a TLS-terminating
    reverse proxy (the documented docker-compose setup); the AIME_HTTPS direct
    path is handled separately below.

    Thread count matters here because /send streams its reply over a
    long-lived SSE connection that occupies one worker thread for the whole
    generation. The pool must comfortably exceed the expected number of
    concurrent in-flight chats, so the default is generous and tunable via
    AIME_THREADS. (If you ever outgrow a single process you'd move to an async
    server, but then the scheduler/limiters need to move to shared state.)
    """
    threads = int(os.environ.get("AIME_THREADS", "16"))
    from waitress import serve as _waitress_serve
    _waitress_serve(app, host=host, port=port, threads=threads, ident="Aime")


if __name__ == "__main__":
    # Bind defaults to 127.0.0.1 (loopback only). Set AIME_BIND=0.0.0.0 to
    # expose the web UI to other devices on the LAN or to a reverse proxy on
    # another host. Anyone who can reach the bind address will be able to hit
    # /login, and unless TLS terminates in front of it the session cookie
    # travels in cleartext — terminate TLS at a proxy (recommended) or use
    # AIME_HTTPS=1 for the direct self-signed path.
    host = os.environ.get("AIME_BIND", "127.0.0.1")
    port = int(os.environ.get("AIME_PORT", "5000"))
    # Start the background scheduler (scheduled agents + event reminders). Safe
    # here because the deployment is a single process — one loop, no
    # double-fire. Disabled with AIME_SCHEDULER=0.
    _start_scheduler()
    if int(os.environ.get("AIME_HTTPS", "0")):
        # Direct TLS with a persistent self-signed cert — for microphone/voice
        # input from phones on the LAN (browsers require a secure context).
        # waitress can't terminate TLS, so this specific mode uses Werkzeug's
        # server with threaded=True (a thread per request).
        ssl_context = _load_or_create_tls_context()
        app.run(host=host, port=port, threaded=True, debug=False,
                ssl_context=ssl_context)
    elif _env_bool("AIME_DEV_SERVER", "0"):
        # Opt-in escape hatch back to the Werkzeug dev server (e.g. for the
        # interactive reloader while developing). Not for production.
        app.run(host=host, port=port, threaded=True, debug=False)
    else:
        # Production default: waitress (see _serve_production). Fall back to
        # the dev server only if waitress isn't installed, with a clear warning
        # so a prod box doesn't silently run the wrong server.
        try:
            _serve_production(host, port)
        except ImportError:
            sys.stderr.write(
                "WARNING: waitress is not installed; falling back to the "
                "Werkzeug development server, which is not suitable for "
                "production. Install waitress (pip install waitress) or set "
                "AIME_DEV_SERVER=1 to silence this.\n"
            )
            app.run(host=host, port=port, threaded=True, debug=False)
