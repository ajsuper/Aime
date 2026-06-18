"""Provider-agnostic agent backend.

Decoupled from any UI so it can be reused by alternate front-ends. The UI
only ever talks to an `AgentBackend`; concrete implementations live below.
"""

import json
import logging
import os
import hashlib
import threading
import datetime
import zoneinfo
from dataclasses import dataclass
from typing import Iterator, Literal, Protocol, runtime_checkable

from anthropic import Anthropic, BadRequestError
from cryptography.exceptions import InvalidTag

logger = logging.getLogger(__name__)

# `aime.encryption` is imported lazily at the bottom of this file. Importing
# the `aime` package eagerly here would deadlock: aime/__init__.py imports
# `controller`, which imports `AgentBackend`/`BackendEvent`/`SessionInfo`
# from this module — defined below.

# Suffix used for on-disk conversation files. Each is AES-GCM encrypted
# under the owning user's DEK; the bytes are not JSON anymore.
_CONV_SUFFIX = ".json.enc"

# Sentinel at the very start of a recovery-flattened message (see
# AnthropicMessagesBackend._recover_history). Lets history replay render such
# a conversation as a short recovery notice instead of dumping the condensed
# transcript into one giant verbatim bubble.
RECOVERY_MARKER = "[Aime conversation recovered]"


def _jsonable(obj):
    """Recursively coerce `obj` into something json.dump can handle.

    Provider SDKs hand back pydantic models (e.g. WebSearchResultBlock) that
    are not JSON serializable. Anything we can't convert cleanly is stringified
    as a last resort so a single odd block can never poison the message list.
    """
    if obj is None or isinstance(obj, (bool, int, float, str)):
        return obj
    if isinstance(obj, dict):
        return {k: _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_jsonable(v) for v in obj]
    # pydantic v2 / v1 models
    for attr in ("model_dump", "dict"):
        method = getattr(obj, attr, None)
        if callable(method):
            try:
                return _jsonable(method())
            except Exception:
                pass
    try:
        json.dumps(obj)
        return obj
    except (TypeError, ValueError):
        return str(obj)


class _ClockTagStripper:
    """Removes ``<clock ...>...</clock>`` spans from a streamed text block.

    The backend appends a volatile ``<clock silent>…</clock>`` block after the
    cache breakpoint each turn (see ``_date_block``). The model occasionally
    parrots that exact tag back into its reply. The tag is system plumbing the
    user must never see, so we filter it out of the assistant text.

    Because text arrives as deltas, the tag can be split across chunks. This
    runs a tiny state machine that holds back only the minimal tail that could
    still be the start of an opening tag (or a partial closing tag while inside
    one), so ordinary text — including unrelated ``<`` characters — flows
    through with at most a few characters of latency. Call ``feed`` per delta
    and ``flush`` once at block end."""

    _OPEN = "<clock"
    _CLOSE = "</clock>"

    def __init__(self) -> None:
        self._buf = ""
        self._in_tag = False  # seen <clock, waiting for </clock>

    @staticmethod
    def _suffix_prefix_len(s: str, target: str) -> int:
        """Longest k where the last k chars of s equal the first k of target."""
        for k in range(min(len(s), len(target)), 0, -1):
            if s[-k:] == target[:k]:
                return k
        return 0

    def feed(self, text: str) -> str:
        self._buf += text
        out: list[str] = []
        while self._buf:
            if self._in_tag:
                idx = self._buf.find(self._CLOSE)
                if idx == -1:
                    # No close yet: discard the body, but keep a tail that
                    # could be a partial </clock> spanning the next delta.
                    keep = self._suffix_prefix_len(self._buf, self._CLOSE)
                    self._buf = self._buf[len(self._buf) - keep:] if keep else ""
                    break
                self._buf = self._buf[idx + len(self._CLOSE):]
                self._in_tag = False
                continue
            idx = self._buf.find(self._OPEN)
            if idx == -1:
                # No open tag: emit everything except a tail that could be a
                # partial <clock spanning the next delta.
                keep = self._suffix_prefix_len(self._buf, self._OPEN)
                cut = len(self._buf) - keep
                out.append(self._buf[:cut])
                self._buf = self._buf[cut:]
                break
            out.append(self._buf[:idx])
            self._buf = self._buf[idx:]  # now starts with <clock…
            self._in_tag = True
        return "".join(out)

    def flush(self) -> str:
        """Drain held-back text at block end. A buffer that's still mid-tag was
        an unterminated <clock and is dropped; anything else was a false alarm
        (a real ``<`` that never became a tag) and is emitted."""
        if self._in_tag:
            self._buf = ""
            return ""
        tail, self._buf = self._buf, ""
        return tail


# ============================================================================
# Provider-agnostic agent backend
# ============================================================================
#
# The UI only ever talks to a `AgentBackend`. Anthropic-specific types,
# session lifecycle, and event-stream parsing live behind this interface so
# swapping to a different provider (Anthropic Messages API, OpenAI,
# self-hosted, etc.) only requires writing a new concrete backend class.
#
#   UI ──submit(BackendEvent)──> Backend ──provider API──> model
#   UI <──stream() yields ──── Backend <──provider events
#

EventKind = Literal[
    # UI → backend (passed to submit)
    "user_send_message",
    "system_send_message",
    "tool_send_response",
    # backend → UI (yielded from stream)
    "assistant_send_text",
    "assistant_text_delta",
    "assistant_text_end",
    "assistant_thinking",
    "assistant_use_tool",
    "turn_end",
    "error",
    "session_terminated",
    # A structurally broken history was flattened so the turn could proceed.
    "history_recovered",
    # The router picked a model for the next turn. Carried in `text` as the
    # label ("haiku" or "sonnet"); the controller surfaces this only when
    # verbose mode is on.
    "turn_routing",
    # The user's usage budget crossed a notification threshold this turn
    # (running low, or spent). Carried in `text` as the state ("notify_low" /
    # "over") with the budget snapshot in `tool_result`. See aime.quota.
    "usage_notice",
]


@dataclass
class BackendEvent:
    kind: EventKind
    text: str | None = None
    tool_name: str | None = None
    tool_input: dict | None = None
    tool_use_id: str | None = None
    tool_result: dict | None = None
    # If True (default), the UI is expected to execute the tool locally and
    # submit a `tool_send_response` event back. Server-side tools handled
    # entirely by the provider set this to False (display only).
    expects_response: bool = True
    stop_reason: str | None = None
    error: str | None = None
    # Optional image attachments for user_send_message events. Each entry:
    #   {"media_type": "image/png" | "image/jpeg" | ..., "data": "<base64>"}
    images: list[dict] | None = None


@dataclass
class SessionInfo:
    """A resumable conversation, as surfaced to session pickers in the UI.

    Deliberately provider-neutral: just enough to display the conversation in
    a list and pass `id` back to `load_session()`.
    """
    id: str
    summary: str = ""
    saved_at: str = ""


@runtime_checkable
class AgentBackend(Protocol):
    """Provider-agnostic interface for an agentic conversation backend."""

    def new_session(self) -> str:
        """Start a fresh session. Returns the session id."""
        ...

    def list_sessions(self) -> list[SessionInfo]:
        """Return resumable sessions, most-recently-saved first. Backends with
        no enumerable history return an empty list."""
        ...

    def load_session(self, session_id: str) -> None:
        """Resume a previously-started session by id."""
        ...

    def messages_snapshot(self) -> list[dict]:
        """Provider-native message dicts for the active session, in order.
        Used by the UI to replay history into the transcript on /load.
        Backends without an enumerable history return an empty list."""
        ...

    def reset(self) -> None:
        """Terminate the current session and start a new one."""
        ...

    def set_session_context(self, text: str) -> None:
        """Attach session-scoped context that should accompany every turn
        without entering the message history. Cleared on new_session/load."""
        ...

    def set_client_timezone(self, tz: str) -> None:
        """Record the client's IANA timezone (e.g. 'America/New_York') so any
        per-turn date/time the model sees reflects the user's local time
        rather than the server's. Empty string => server-local time."""
        ...

    def submit(self, event: BackendEvent) -> None:
        """Push a user/system/tool-result event into the conversation."""
        ...

    def delete_session(self, session_id: str) -> None:
        """Delete a saved session by id. No-op for backends without enumerable
        history. If the deleted session is currently active, also resets to a
        fresh session."""
        ...

    def delete_all_sessions(self) -> None:
        """Delete every saved session and reset the active conversation. No-op
        for backends without enumerable history."""
        ...

    def stream(self) -> Iterator[BackendEvent]:
        """Yield normalized events from the model. Blocks until the session
        terminates; meant to run on a worker thread."""
        ...

    def shutdown(self) -> None:
        """Clean up any provider resources."""
        ...

    def interrupt_turn(self) -> None:
        """Best-effort cancel of the in-flight assistant turn. Any partial
        assistant response is discarded so the next turn starts from a clean
        history. No-op if no turn is active or the backend cannot interrupt
        in the middle of a stream."""
        ...


# Safe to import now: AgentBackend / BackendEvent / SessionInfo are defined,
# so the aime package's eager import of controller can resolve them.
import aime.encryption as _enc
import aime.dateformat as dateformat
from aime import graphics as _graphics


class AnthropicMessagesBackend:
    """Anthropic Messages API implementation of AgentBackend.

    Maintains the conversation history client-side as a list of message dicts
    and drives the agent loop by calling messages.stream() once per assistant
    turn. Tool uses are surfaced to the UI, which executes them locally and
    submits results back via tool_send_response; once every tool_use in the
    last assistant turn has a matching result, the loop continues.
    """

    # --- compaction tuning ---
    # Once the history grows past this many messages, the oldest ones are
    # folded into a single summary message at the start of the next turn.
    # Each compaction rewrites the message prefix, invalidating the prompt
    # cache for the whole history — so compact infrequently and in large
    # batches to keep a warmed cache alive across as many turns as possible.
    COMPACT_TRIGGER_MSGS = 32
    # How many of the oldest messages to fold into the summary per compaction
    # pass. The cut point is nudged forward from here to land on a safe
    # boundary (see _maybe_compact).
    COMPACT_BATCH_MSGS = 16
    # Cheap model used for the summarization call.
    COMPACT_MODEL = "claude-haiku-4-5-20251001"
    # Prefix that marks a message as a compaction summary (so later passes can
    # detect and merge it instead of re-summarizing from scratch).
    _SUMMARY_MARKER = "[Conversation summary so far]"

    def __init__(
        self,
        system_prompt: str,
        model: str,
        schema_files: list[str],
        conversations_dir: str,
        dek: bytes,
        max_tokens: int = 8192,
        usage_label: str | None = None,
        router=None,
        web_search_schema: str | None = None,
        terminal_tool_schema: "str | dict | None" = None,
        persist_enabled: bool = True,
        usage_source: str = "interactive",
        quota=None,
    ):
        self._client = Anthropic(max_retries=3)
        self._system_prompt = system_prompt
        # `model` is the *default* / fallback. When a router is attached, each
        # turn picks between Haiku and Sonnet; the default is what we fall
        # back to (and what continuations inside a tool loop keep using).
        self._model = model
        self._router = router
        # Sticky pick for the current tool-loop. Set when a fresh user turn
        # starts and the router picks; reused for every continuation turn
        # (the agent loop after tool_results) until the loop ends. None means
        # "this is a fresh user turn, ask the router".
        self._current_turn_model: str | None = None
        self._current_turn_label: str | None = None
        self._schema_files = schema_files
        self._max_tokens = max_tokens
        # Identifier (username) attributed to this backend's API usage in the
        # opt-in usage log. None for unattributed callers. Whether it is
        # actually written is a separate opt-in inside aime.usage.
        self._usage_label = usage_label
        # Tags every usage record from this backend with what drove it: a live
        # chat ("interactive") or a background-agent run ("agent"). Lets the
        # dashboard separate a user's autonomous-agent cost from their
        # live-chat cost. Non-identifying, so unconditionally recorded.
        self._usage_source = usage_source or "interactive"
        # Optional per-user usage budget (aime.quota.QuotaMeter). When attached,
        # every API call's real cost is debited here (in _record_usage) and a
        # usage_notice is emitted from _run_turn when the budget crosses a
        # threshold. None when usage limits are disarmed (AIME_ACCESS_MODE=open).
        self._quota = quota
        # Last threshold state we notified the user about, so a "running low" /
        # "over" banner fires on the *transition* rather than on every turn.
        self._last_usage_decision = None
        self._usage_notice_pending: dict | None = None
        # Per-user state: where this user's encrypted conversation files live
        # and the data key that decrypts them. Both are required for any IO.
        self._conversations_dir = conversations_dir
        self._dek = dek
        self._tools = [self._load_schema(p) for p in schema_files]
        # Web search is offloaded to a Haiku sub-agent via a small client-side
        # `WebSearch` tool (see aime.web_search_agent and the controller's tool
        # dispatch). When enabled, its schema loads like any other; the native
        # server-side web_search tool is intentionally NOT exposed to the
        # conversational model — that keeps bulky search results out of this
        # model's re-cached context.
        if web_search_schema:
            self._tools.insert(0, self._load_schema(web_search_schema))
        # System prompt and tool schemas are byte-identical on every turn, so
        # mark them as prompt-cache breakpoints with a 1-hour TTL — across the
        # gaps typical of personal-assistant chat the 5-min default TTL would
        # expire repeatedly and force re-billing the whole prefix.
        self._system_prompt_block = {
            "type": "text",
            "text": system_prompt,
            "cache_control": {"type": "ephemeral", "ttl": "1h"},
        }
        if self._tools:
            self._tools[-1] = {
                **self._tools[-1],
                "cache_control": {"type": "ephemeral", "ttl": "1h"},
            }
        # The "terminal tool" is special: it is offered to the model only while
        # a particular flow is active (CompleteOnboarding during first-time
        # onboarding; SubmitResult for the whole life of a background-agent run
        # — see set_terminal_tool_active). It is deliberately NOT part of
        # self._tools / the cached prefix — _tools_for_turn appends it AFTER the
        # cache breakpoint, so toggling it never invalidates the cached prefix.
        # Accepts a schema *path* (loaded here) or a pre-built raw schema dict
        # (e.g. a per-agent SubmitResult whose `result` shape varies by task).
        if terminal_tool_schema is None:
            self._terminal_tool = None
        elif isinstance(terminal_tool_schema, str):
            self._terminal_tool = self._load_schema(terminal_tool_schema)
        else:
            self._terminal_tool = self._schema_to_tool(terminal_tool_schema)
        self._terminal_tool_active = False
        # When False, _persist() is a no-op: the conversation lives only in
        # memory for the life of the process. Background-agent runs use this so
        # they never litter the user's saved-conversation list; their audit
        # trail is the separate run record (see aime.agents.store).
        self._persist_enabled = persist_enabled
        # Per-session dynamic context (e.g. bootstrapped topic contents). Lives
        # in the system array rather than the message history so it stays out
        # of compaction and doesn't get replayed inside user turns.
        self._session_context: str = ""
        # Client's IANA timezone, refreshed from each /send. Drives the
        # per-turn date block so the model sees the *user's* local time
        # rather than the server's. Empty => fall back to server-local time.
        self._client_tz: str = ""
        # The user's date/time *display* preferences (see aime.dateformat),
        # refreshed from each /send alongside the timezone. They tell the model
        # which format to write dates/times in (the per-turn date block carries
        # them); None => fall back to an unambiguous default keyed off the tz.
        self._date_format: str | None = None
        self._time_format: str | None = None

        self._session_id: str | None = None
        # One-sentence human-readable description of the session, shown in
        # session pickers. Generated by Haiku from the user's first prompt and
        # refreshed during compaction. Persisted in the session file.
        self._summary: str = ""
        # True while a background _generate_title thread is in flight, so
        # submit() doesn't spawn a duplicate one on the next message.
        self._title_generating = False
        # True while a background compaction thread is in flight. Compaction
        # makes two sequential Haiku calls (summary + title refresh) so it's
        # run off the turn thread; this flag prevents stacking up duplicates
        # when several turns finish before the first compaction completes.
        self._compacting = False
        self._messages: list[dict] = []
        self._pending_tool_results: list[dict] = []
        self._expected_tool_use_ids: set[str] = set()
        # Maps a live tool_use id to (tool_name, model) so that when the
        # matching tool_result lands we can attribute its byte size to the
        # right tool and price it at the turn's model rate. Entries are
        # popped on tool_send_response; an interrupt cleanup clears the dict.
        self._tool_attrib: dict[str, tuple[str, str]] = {}
        self._turn_trigger = threading.Event()
        self._terminated = threading.Event()
        # Set by interrupt_turn() to stop the current _run_turn loop without
        # terminating the whole stream. Cleared at the start of each turn.
        self._interrupted = threading.Event()
        # Bumped on every reset(). A stream loop captures the epoch when it
        # starts; once it no longer matches, that loop is stale and must exit.
        # This is what lets reset() re-arm _terminated/_turn_trigger for the
        # *new* stream loop without a still-running old loop mistaking the
        # re-armed (cleared) state for "keep going".
        self._epoch = 0
        self._lock = threading.Lock()
        # Serializes the whole _persist() operation. The turn loop and the
        # background title thread both persist concurrently; without this they
        # race on the temp file and can clobber a freshly-written title with a
        # stale snapshot.
        self._persist_lock = threading.Lock()

    # --- AgentBackend interface ---

    @property
    def session_id(self) -> str | None:
        """The on-disk name of the active conversation file, or None before a
        session is opened. Read by the controller so sub-agent usage (e.g. the
        web-search Haiku call) can be attributed to the same conversation."""
        return self._session_id

    @property
    def conversations_dir(self) -> str:
        """Per-user directory where this backend's encrypted conversation files
        live. Also the natural home for small per-user state files (e.g. the
        onboarding-complete flag)."""
        return self._conversations_dir

    def set_terminal_tool_active(self, active: bool) -> None:
        """Offer (or withdraw) the configured terminal tool to the model.

        For the interactive backend this is CompleteOnboarding: the controller
        turns it on while first-time onboarding is in flight and off once the
        model has called it (or on reset/load). For a background-agent backend
        it is SubmitResult, turned on for the whole run. No-op if no terminal
        tool schema was configured."""
        with self._lock:
            self._terminal_tool_active = bool(active) and self._terminal_tool is not None

    def _tools_for_turn(self) -> list[dict]:
        """The cached base tools, plus the terminal tool appended after the
        cache breakpoint while it is active. Appending (rather than rebuilding)
        keeps the cached tool prefix byte-stable."""
        with self._lock:
            active = self._terminal_tool_active
        if active and self._terminal_tool is not None:
            return [*self._tools, self._terminal_tool]
        return self._tools

    def set_session_context(self, text: str) -> None:
        """Attach session-scoped context (e.g. bootstrapped topic contents) as
        an extra cached system block instead of embedding it in the first user
        message. Keeps it out of message history (no replay, no compaction)
        while still benefiting from prompt caching."""
        with self._lock:
            self._session_context = text or ""

    def set_client_timezone(self, tz: str) -> None:
        """Record the client's IANA timezone so the per-turn date block
        reflects the user's local time. Survives new_session/load — a user's
        timezone is a property of the client, not of any one conversation."""
        with self._lock:
            self._client_tz = tz or ""

    def set_client_date_prefs(
        self, date_format: str | None, time_format: str | None
    ) -> None:
        """Record the user's date/time display preferences (see aime.dateformat),
        surfaced to the model in the per-turn date block. Like the timezone,
        these belong to the client, not a conversation, so they survive
        new_session/load. An unknown value is normalized to None so the block
        falls back to the unambiguous default."""
        df = (date_format or "").strip() or None
        tf = (time_format or "").strip() or None
        with self._lock:
            self._date_format = df if df in dateformat.DATE_PATTERNS else None
            self._time_format = tf if tf in dateformat.TIME_FORMATS else None

    def _build_system(self) -> list[dict]:
        """Assemble the system array for this turn.

        Everything here is part of the cached prefix, so it must be byte-stable
        across turns. The volatile date/time block is *not* here — it would sit
        between the cached system prefix and the message history, busting the
        message-history cache on every minute change. It is appended after the
        message-cache breakpoint instead (see _cacheable_messages).
        """
        blocks: list[dict] = [self._system_prompt_block]
        with self._lock:
            ctx = self._session_context
        if ctx:
            blocks.append({
                "type": "text",
                "text": ctx,
                "cache_control": {"type": "ephemeral", "ttl": "1h"},
            })
        return blocks

    def _date_block(self) -> dict:
        """The volatile 'accurate date' block. Carries minute-granular time, so
        it changes constantly — it must only ever be placed *after* a cache
        breakpoint, never inside a cached prefix.

        Formatted in the client's timezone when one is known, so the model
        sees the *user's* local time; an unset or unrecognised zone falls
        back to server-local time.

        The "now" is spelled out (weekday + full month name) so it is
        unambiguous to reason from — "04/06" never has to be guessed as April
        or June. It then states the user's chosen *display* format and shows
        the current instant rendered in it, so the model both knows the format
        and has a worked example to copy when it writes dates/times back to the
        user. The system prompt teaches what to *do* with all this (write dates
        in the user's format, keep tool fields in DD/MM/YYYY, never acknowledge
        the tag), keeping that explainer in the cached prefix."""
        tz = self._client_tz
        now = None
        if tz:
            try:
                now = datetime.datetime.now(zoneinfo.ZoneInfo(tz))
            except Exception:
                now = None  # unknown zone / missing tzdata — fall back below
        if now is None:
            now = datetime.datetime.now()
        day_names = ["Monday", "Tuesday", "Wednesday", "Thursday",
                     "Friday", "Saturday", "Sunday"]
        month_names = ["January", "February", "March", "April", "May", "June",
                       "July", "August", "September", "October", "November",
                       "December"]
        anchor = (
            f"{day_names[now.weekday()]}, {now.day} {month_names[now.month - 1]} "
            f"{now.year}, {now.strftime('%H:%M')}"
        )
        date_fmt = self._date_format or dateformat.default_date_format(tz or None)
        time_fmt = self._time_format or dateformat.DEFAULT_TIME_FORMAT
        example = (
            f"{dateformat.render_date(now.date(), date_fmt)}, "
            f"{dateformat.render_time(now.time(), time_fmt)}"
        )
        time_label = "12-hour" if time_fmt == "12" else "24-hour"
        return {
            "type": "text",
            "text": (
                f"<clock silent>{anchor}. This user reads dates as {date_fmt} "
                f"and times as {time_label} — now is \"{example}\" in their "
                "format. System info, don't repeat to user</clock>"
            ),
        }

    def new_session(self) -> str:
        with self._lock:
            self._messages = []
            self._summary = ""
            self._title_generating = False
            self._pending_tool_results = []
            self._expected_tool_use_ids = set()
            self._tool_attrib.clear()
            self._session_context = ""
            self._current_turn_model = None
            self._current_turn_label = None
        self._terminated.clear()
        self._turn_trigger.clear()
        stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        self._session_id = f"msgs-{stamp}-" + hashlib.sha1(os.urandom(8)).hexdigest()[:8]
        # Don't persist yet: the session is empty. The first submit() will
        # write the file once the user has actually said something, so that
        # /reset (or app launch) doesn't litter conversations/ with empty
        # placeholders.
        return self._session_id

    def messages_snapshot(self) -> list[dict]:
        """Copy of the current message list, safe for the UI to iterate
        without holding the backend lock."""
        with self._lock:
            return list(self._messages)

    def load_session(self, session_id: str) -> None:
        path = self._session_path(session_id)
        with open(path, "rb") as f:
            blob = f.read()
        plaintext = _enc.decrypt_blob(self._dek, blob, aad=session_id.encode("utf-8"))
        data = json.loads(plaintext)
        # Same epoch-bump dance as reset(): the previous stream worker thread
        # (Textual can't actually kill thread workers — exclusive=True only
        # flags them) must observe its captured epoch is stale and exit, or
        # we'd have two stream loops sharing self._messages and answering
        # every prompt twice.
        self._terminate_active_stream()
        with self._lock:
            self._session_id = session_id
            self._messages = data.get("messages", [])
            saved_summary = data.get("summary", "")
            self._summary = "" if saved_summary in ("", "none") else saved_summary
            self._title_generating = False
            self._pending_tool_results = []
            self._expected_tool_use_ids = set()
            self._tool_attrib.clear()
            self._session_context = ""
            self._current_turn_model = None
            self._current_turn_label = None
        self._terminated.clear()
        self._turn_trigger.clear()

    def _session_path(self, session_id: str) -> str:
        os.makedirs(self._conversations_dir, exist_ok=True)
        return os.path.join(self._conversations_dir, f"{session_id}{_CONV_SUFFIX}")

    def list_sessions(self) -> list[SessionInfo]:
        # History lives as one encrypted file per session on disk; enumerate
        # them and surface just the id + summary the UI needs. A file we
        # can't read, decrypt, or parse is skipped rather than failing the
        # whole listing (e.g. truncated, wrong key, or stray junk).
        sessions: list[SessionInfo] = []
        try:
            names = os.listdir(self._conversations_dir)
        except OSError:
            return []
        for name in names:
            if not name.endswith(_CONV_SUFFIX):
                continue
            session_id = name[: -len(_CONV_SUFFIX)]
            try:
                with open(os.path.join(self._conversations_dir, name), "rb") as f:
                    blob = f.read()
                plaintext = _enc.decrypt_blob(
                    self._dek, blob, aad=session_id.encode("utf-8")
                )
                data = json.loads(plaintext)
            except (OSError, ValueError, InvalidTag):
                continue
            summary = data.get("summary", "")
            if summary == "none":
                summary = ""
            sessions.append(SessionInfo(
                id=data.get("id") or session_id,
                summary=summary,
                saved_at=data.get("saved_at", ""),
            ))
        sessions.sort(key=lambda s: s.saved_at, reverse=True)
        return sessions

    def _persist(self) -> None:
        if not self._session_id or not self._persist_enabled:
            return
        # Hold _persist_lock across the snapshot *and* the file write, so a
        # slow writer can't os.replace() a stale snapshot over a newer one
        # (e.g. overwriting the title with "none"). The temp file name is made
        # unique per call as a second line of defense against collisions.
        with self._persist_lock:
            try:
                path = self._session_path(self._session_id)
                with self._lock:
                    snapshot = {
                        "id": self._session_id,
                        "saved_at": datetime.datetime.now().isoformat(timespec="seconds"),
                        "summary": self._summary or "none",
                        "messages": list(self._messages),
                    }
                tmp = f"{path}.{os.getpid()}.{threading.get_ident()}.tmp"
                plaintext = json.dumps(snapshot, default=_jsonable).encode("utf-8")
                blob = _enc.encrypt_blob(
                    self._dek, plaintext, aad=self._session_id.encode("utf-8")
                )
                with open(tmp, "wb") as f:
                    f.write(blob)
                os.replace(tmp, path)
            except (OSError, TypeError, ValueError):
                # OSError: disk/path issues. TypeError/ValueError: a content
                # block slipped through that even `default=_jsonable` couldn't
                # coerce — drop the write rather than crash the turn.
                pass

    def _find_tool_use_block(self, tool_use_id: str) -> dict | None:
        """The stored assistant `tool_use` block with this id, or None. Caller
        holds `self._lock`."""
        for msg in reversed(self._messages):
            if msg.get("role") != "assistant":
                continue
            content = msg.get("content")
            if not isinstance(content, list):
                continue
            for block in content:
                if (isinstance(block, dict)
                        and block.get("type") == "tool_use"
                        and block.get("id") == tool_use_id):
                    return block
        return None

    def register_graphic(
        self, tool_use_id: str, graphic_id: str, source: str,
        summary: str | None = None,
    ) -> bool:
        """Stamp a freshly-drawn CreateGraphics block with its store id and the
        cleaned, render-ready source, then persist. The canonical copy lives in
        the user's GraphicStore (which allocated ``graphic_id``); this history
        stamp is what replay re-renders a session's graphics from on /load, and
        what the send-time strip (see _cacheable_messages) references by id while
        keeping the source out of the model's per-turn context. Returns False if
        the block is gone.

        Safe from the stream-generator thread: the turn loop is suspended on the
        tool_use `yield` when the controller calls this, so nothing else mutates
        `self._messages`."""
        with self._lock:
            block = self._find_tool_use_block(tool_use_id)
            if block is None:
                return False
            inp = block.get("input")
            if not isinstance(inp, dict):
                inp = {}
                block["input"] = inp
            inp["source"] = source
            inp["graphic_id"] = graphic_id
            if summary is not None:
                inp["summary"] = summary
        self._persist()
        return True

    def interrupt_turn(self) -> None:
        """Signal that the in-flight or pending model turn should be
        aborted. Handles three sub-states:

          * mid-stream — `_run_turn`'s event loop sees the flag and breaks
            on the next iteration;
          * blocked on `_turn_trigger` waiting for tool_results (the model
            ended with `stop_reason=tool_use`) — `_turn_trigger.set()`
            wakes the outer `stream()` loop, which observes the flag and
            performs between-turn cleanup;
          * tool_results just submitted, next turn about to start — same
            wake-up path as the previous case.

        Cleanup details live next to where each path observes the flag.
        """
        self._interrupted.set()
        # Wake the outer stream loop if it's between turns.
        self._turn_trigger.set()

    def _terminate_active_stream(self) -> None:
        # Wake the current stream loop and force it to observe its captured
        # epoch is stale, so it exits with session_terminated before we mutate
        # the shared session state. Used by reset() and load_session() — both
        # swap the conversation out from under any in-flight stream worker.
        with self._lock:
            self._epoch += 1
        self._terminated.set()
        self._turn_trigger.set()

    def reset(self) -> None:
        self._terminate_active_stream()
        self.new_session()

    def delete_session(self, session_id: str) -> None:
        if not session_id:
            return
        try:
            os.remove(self._session_path(session_id))
        except OSError:
            pass

    def delete_all_sessions(self) -> None:
        try:
            names = os.listdir(self._conversations_dir)
        except OSError:
            return
        for name in names:
            if not name.endswith(_CONV_SUFFIX):
                continue
            try:
                os.remove(os.path.join(self._conversations_dir, name))
            except OSError:
                pass

    def shutdown(self) -> None:
        self._terminated.set()
        self._turn_trigger.set()

    def submit(self, event: BackendEvent) -> None:
        if event.kind in ("user_send_message", "system_send_message"):
            content: list[dict] = []
            for img in (event.images or []):
                content.append({
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": img.get("media_type") or "image/png",
                        "data": img.get("data") or "",
                    },
                })
            # The API rejects empty text blocks, so only append one when the
            # user actually typed something. Image-only sends are valid as
            # long as at least one image block is present above.
            if event.text:
                content.append({"type": "text", "text": event.text})
            with self._lock:
                self._messages.append({
                    "role": "user",
                    "content": content,
                })
                # Generate the session description as soon as there is at
                # least one user message and the session has no title yet
                # (covers fresh sessions and ones resumed from a persisted
                # "none"). _title_generating guards against spawning a second
                # Haiku thread while one is already in flight.
                user_texts = [
                    block["text"]
                    for msg in self._messages
                    if msg["role"] == "user"
                    for block in msg["content"]
                    if isinstance(block, dict) and block.get("type") == "text"
                ]
                needs_title = (
                    not self._summary
                    and len(user_texts) >= 1
                    and not self._title_generating
                )
                if needs_title:
                    self._title_generating = True
                title_prompt = "\n\n".join(user_texts)
            self._persist()
            # Off the turn thread so the Haiku call never delays the response.
            if needs_title and event.kind == "user_send_message":
                threading.Thread(
                    target=self._generate_title,
                    args=(title_prompt,),
                    daemon=True,
                ).start()
            self._turn_trigger.set()
        elif event.kind == "tool_send_response":
            result = event.tool_result
            if isinstance(result, (dict, list)):
                content_text = json.dumps(result)
            else:
                content_text = str(result if result is not None else "")
            with self._lock:
                # Drop stale tool results. After an interrupt cleanup,
                # `_expected_tool_use_ids` is cleared but a slow tool
                # already executing on the controller's thread may still
                # call back here. Without this guard, the late result
                # would later be flushed as a user message and trigger
                # an unwanted next turn.
                if event.tool_use_id not in self._expected_tool_use_ids:
                    return
                self._pending_tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": event.tool_use_id,
                    "content": content_text,
                })
                self._expected_tool_use_ids.discard(event.tool_use_id)
                # Buffer only. The user message holding these tool_results
                # is appended by `_run_turn` after the assistant stream
                # finishes. Flushing eagerly here breaks parallel tool
                # calls: with two tool_use blocks A and B, A's result
                # arrives (and would flush as a user message) before B
                # even streams out, which leaves B landing in an assistant
                # message that already has a following user message — i.e.
                # an orphan tool_use.
                attrib = self._tool_attrib.pop(event.tool_use_id, None)
            if attrib is not None:
                # Recorded after releasing the lock: usage IO must not block
                # the agent loop, and a stats failure must never break a turn.
                try:
                    import aime.usage as _usage
                    tname, tmodel = attrib
                    _usage.record_tool_use(
                        self._usage_label,
                        tname,
                        "client",
                        model=tmodel,
                        result_bytes=len(content_text.encode("utf-8")),
                        session_id=self._session_id,
                        source=self._usage_source,
                    )
                except Exception:
                    pass
        else:
            raise ValueError(f"submit() does not accept event kind: {event.kind}")

    def stream(self) -> Iterator[BackendEvent]:
        with self._lock:
            my_epoch = self._epoch
        while True:
            self._turn_trigger.wait()
            # Check staleness/termination *before* clearing the trigger: a
            # stale loop must leave the trigger intact so the fresh loop still
            # sees the wake-up meant for it.
            with self._lock:
                stale = self._epoch != my_epoch
            if stale or self._terminated.is_set():
                yield BackendEvent(kind="session_terminated")
                return
            self._turn_trigger.clear()
            # Between-turn interrupt (state B/C): we were sleeping on the
            # trigger and interrupt_turn() woke us. Clean up dangling tool
            # state and emit a turn_end interrupted instead of starting a
            # fresh assistant turn.
            if self._interrupted.is_set():
                self._cleanup_interrupted_turn(None)
                self._interrupted.clear()
                yield BackendEvent(kind="turn_end", stop_reason="interrupted")
                continue
            # Refuse to start a new turn if the message list doesn't end on
            # a user message. After an interrupt the cleanup leaves an
            # assistant stub at the tail (and interrupt_turn() pre-sets
            # _turn_trigger to wake state B/C); without this guard the
            # outer wait() would return immediately on that pre-set trigger
            # and _run_turn would ship an assistant-tailed snapshot to the
            # API, which fails with "assistant message prefill" 400.
            # Also guards against the same race after reset or load.
            with self._lock:
                has_messages = bool(self._messages)
                last_role = self._messages[-1].get("role") if has_messages else None
            if not has_messages or last_role != "user":
                continue
            yield from self._turn_with_recovery()

    # --- internal ---

    def _turn_with_recovery(self) -> Iterator[BackendEvent]:
        """Run one assistant turn, recovering from a structurally broken
        history instead of letting it brick the conversation.

        Two layers of defense:
          * `_run_turn` proactively validates the history and flattens it
            (see `_recover_history`) before sending if its cheap checker
            flags a problem — so a known-bad history never reaches the API.
          * This wrapper additionally catches a 400 the validator didn't
            anticipate (an incompatible attachment, an orphan tool pair we
            didn't model), flattens, and retries the turn exactly once.

        Anything that isn't a malformed-request 400 (network blips, 429s,
        500s) is surfaced as a plain error — those are transient and must
        not trigger a destructive history rewrite.
        """
        try:
            yield from self._run_turn()
            return
        except Exception as exc:
            if not self._is_malformed_history_error(exc):
                self._discard_failed_assistant_placeholder()
                yield BackendEvent(kind="error", error=str(exc))
                yield BackendEvent(kind="turn_end", stop_reason="error")
                return
            # The API rejected the request itself as malformed. Flatten the
            # history to a single valid message and retry the turn once.
            self._discard_failed_assistant_placeholder()
            reason = f"the conversation could not be processed ({exc})"
            self._recover_history(reason)
            yield BackendEvent(kind="history_recovered", text=reason)
        try:
            yield from self._run_turn()
        except Exception as exc:
            self._discard_failed_assistant_placeholder()
            yield BackendEvent(kind="error", error=str(exc))
            yield BackendEvent(kind="turn_end", stop_reason="error")

    @staticmethod
    def _is_malformed_history_error(exc: Exception) -> bool:
        """True when `exc` is the API rejecting the request as structurally
        invalid (HTTP 400) — the signature of a corrupted history — rather
        than a transient network/server failure."""
        return (
            isinstance(exc, BadRequestError)
            or getattr(exc, "status_code", None) == 400
        )

    @staticmethod
    def _history_problem(messages: list[dict]) -> str | None:
        """Cheap structural check of the message history. Returns a short
        description of the first API-invalid condition found, or None when
        the history looks safe to send.

        Deliberately conservative: it only flags conditions the Messages
        API genuinely rejects (bad roles, broken alternation, orphan
        tool_use/tool_result pairs, a web search with no result, empty or
        malformed content) so a healthy conversation is never needlessly
        flattened and stripped of its prompt cache."""
        if not messages:
            return None
        for i, msg in enumerate(messages):
            if not isinstance(msg, dict):
                return "a message is not an object"
            role = msg.get("role")
            if role not in ("user", "assistant"):
                return f"unexpected message role {role!r}"
            if i == 0 and role != "user":
                return "history does not start with a user message"
            if i > 0:
                prev = messages[i - 1]
                prev_role = prev.get("role") if isinstance(prev, dict) else None
                if role == prev_role:
                    return "two consecutive messages share a role"
            content = msg.get("content")
            if isinstance(content, str):
                continue
            if not isinstance(content, list) or not content:
                return "a message has empty or non-list content"
            for blk in content:
                if not isinstance(blk, dict) or not blk.get("type"):
                    return "a message has a malformed content block"
            if role == "assistant":
                # Every tool_use must be answered in the next (user) message.
                tool_use_ids = [b["id"] for b in content
                                if b.get("type") == "tool_use" and b.get("id")]
                if tool_use_ids:
                    nxt = messages[i + 1] if i + 1 < len(messages) else None
                    answered: set = set()
                    if (isinstance(nxt, dict) and nxt.get("role") == "user"
                            and isinstance(nxt.get("content"), list)):
                        answered = {
                            b.get("tool_use_id") for b in nxt["content"]
                            if isinstance(b, dict)
                            and b.get("type") == "tool_result"
                        }
                    if any(tid not in answered for tid in tool_use_ids):
                        return "a tool call has no matching tool result"
                # A server tool use (web search) must carry its own result.
                server_ids = [b["id"] for b in content
                              if b.get("type") == "server_tool_use" and b.get("id")]
                if server_ids:
                    got = {b.get("tool_use_id") for b in content
                           if b.get("type") == "web_search_tool_result"}
                    if any(sid not in got for sid in server_ids):
                        return "a web search has no matching result"
            else:  # user
                results = [b for b in content if b.get("type") == "tool_result"]
                if results:
                    prev = messages[i - 1] if i > 0 else None
                    prev_ids: set = set()
                    if (isinstance(prev, dict) and prev.get("role") == "assistant"
                            and isinstance(prev.get("content"), list)):
                        prev_ids = {
                            b.get("id") for b in prev["content"]
                            if isinstance(b, dict)
                            and b.get("type") in ("tool_use", "server_tool_use")
                        }
                    if any(b.get("tool_use_id") not in prev_ids for b in results):
                        return "a tool result has no matching tool call"
        return None

    def _flatten_messages(self, messages: list[dict]) -> str:
        """Render the message history as a plain-text transcript. Used to
        rebuild a structurally broken history as one valid user message."""
        lines: list[str] = []
        for msg in messages:
            if not isinstance(msg, dict):
                continue
            speaker = "User" if msg.get("role") == "user" else "Aime"
            content = msg.get("content")
            if isinstance(content, str):
                if content.strip():
                    lines.append(f"{speaker}: {content.strip()}")
                continue
            if not isinstance(content, list):
                continue
            for blk in content:
                if not isinstance(blk, dict):
                    continue
                btype = blk.get("type")
                if btype == "text":
                    txt = (blk.get("text") or "").strip()
                    if txt:
                        lines.append(f"{speaker}: {txt}")
                elif btype == "image":
                    lines.append(f"{speaker}: [image attachment]")
                elif btype in ("tool_use", "server_tool_use"):
                    name = blk.get("name") or "a tool"
                    try:
                        inp = json.dumps(blk.get("input") or {}, ensure_ascii=False)
                    except (TypeError, ValueError):
                        inp = "{}"
                    lines.append(f"[Aime used {name}: {inp[:800]}]")
                elif btype == "tool_result":
                    res = blk.get("content")
                    if not isinstance(res, str):
                        try:
                            res = json.dumps(res, ensure_ascii=False, default=str)
                        except (TypeError, ValueError):
                            res = str(res)
                    lines.append(f"[Tool result: {res[:1500]}]")
                elif btype == "web_search_tool_result":
                    lines.append("[Web search results]")
        return "\n\n".join(lines)

    def _recover_history(self, reason: str) -> None:
        """Replace a structurally broken message history with a single
        plain-text user message holding the whole transcript.

        A lone user message is always a valid request, so this can never
        fail to send — it trades the structured tool history (already
        broken) for a conversation the user can keep using. A short note
        is prepended so the model still has the context to explain, calmly,
        what happened if the user asks. The result is persisted so the
        conversation is permanently un-bricked, not just for this turn.

        Caller must NOT hold self._lock."""
        with self._lock:
            transcript = self._flatten_messages(self._messages)
            note = (
                "[System note: the earlier conversation history hit a "
                "technical problem and has been condensed into the transcript "
                "below. Treat it as the full prior context. If the user asks "
                "what happened, about a missing result, or a lost attachment, "
                "briefly and calmly explain there was a technical hiccup and "
                "offer to continue. Do not quote this note verbatim. "
                f"(Internal detail: {reason})]"
            )
            parts = [RECOVERY_MARKER, note]
            if transcript:
                parts.append(transcript)
            body = "\n\n".join(parts)
            self._messages = [{
                "role": "user",
                "content": [{"type": "text", "text": body}],
            }]
            self._pending_tool_results = []
            self._expected_tool_use_ids = set()
            self._tool_attrib.clear()
        self._persist()

    def _discard_failed_assistant_placeholder(self) -> None:
        """Drop the empty assistant message `_run_turn` reserves at the tail
        when a turn raised before producing any output — leaving it would
        itself add an empty-content message (a 400) to the history."""
        with self._lock:
            if (self._messages
                    and isinstance(self._messages[-1], dict)
                    and self._messages[-1].get("role") == "assistant"
                    and not self._messages[-1].get("content")):
                self._messages.pop()

    def _run_turn(self) -> Iterator[BackendEvent]:
        # Fresh interrupt slate per turn — a stale flag from a prior turn
        # must not abort the new one.
        self._interrupted.clear()
        # Proactive corruption guard: a structurally broken history would be
        # rejected by the API with a 400 and brick the conversation. Detect
        # it cheaply and flatten to one valid message before we ever send.
        with self._lock:
            problem = self._history_problem(self._messages)
        if problem:
            self._recover_history(problem)
            yield BackendEvent(kind="history_recovered", text=problem)
        assistant_blocks: list[dict] = []

        with self._lock:
            messages_snapshot = list(self._messages)
            # Reserve the assistant message slot up-front and share the same
            # content list with assistant_blocks. This way, any tool_result
            # submitted by the UI mid-stream (in response to assistant_use_tool)
            # sees a _messages tail that already contains the tool_use block —
            # avoiding the "tool_result without matching tool_use" 400.
            self._messages.append({"role": "assistant", "content": assistant_blocks})
            sticky_model = self._current_turn_model
            sticky_label = self._current_turn_label

        # Routing: a fresh user turn (no sticky pick) asks the router; a
        # continuation inside a tool loop sticks with whatever model started
        # the loop. Downgrading mid-loop would strand tool_use blocks the
        # cheap model didn't plan for and invalidate the prompt cache.
        if sticky_model is None:
            if self._router is not None:
                has_images = self._last_user_has_images(messages_snapshot)
                turn_model, turn_label = self._router.choose(
                    messages_snapshot,
                    is_continuation=False,
                    has_images=has_images,
                    session_id=self._session_id,
                )
            else:
                turn_model, turn_label = self._model, "sonnet"
            with self._lock:
                self._current_turn_model = turn_model
                self._current_turn_label = turn_label
            # Notify the controller of the routing decision. The controller
            # only surfaces this when verbose mode is on; otherwise dropped.
            yield BackendEvent(kind="turn_routing", text=turn_label)
        else:
            turn_model, turn_label = sticky_model, sticky_label

        turn_started = datetime.datetime.now()
        with self._client.messages.stream(
            model=turn_model,
            system=self._build_system(),
            tools=self._tools_for_turn(),
            messages=self._cacheable_messages(messages_snapshot),
            max_tokens=self._max_tokens,
        ) as stream:
            current_text: list[str] = []
            text_stripper = _ClockTagStripper()
            current_tool: dict | None = None
            partial_json = ""

            for event in stream:
                if self._interrupted.is_set():
                    break
                etype = getattr(event, "type", None)
                if etype == "content_block_start":
                    block = event.content_block
                    if block.type in ("tool_use", "server_tool_use"):
                        current_tool = {
                            "type": block.type,
                            "id": block.id,
                            "name": block.name,
                            "input": {},
                        }
                        partial_json = ""
                    elif block.type == "web_search_tool_result":
                        # Server-executed result — preserve in history and
                        # display a one-liner; no response expected from us.
                        result_block = _jsonable({
                            "type": "web_search_tool_result",
                            "tool_use_id": block.tool_use_id,
                            "content": getattr(block, "content", []),
                        })
                        with self._lock:
                            assistant_blocks.append(result_block)
                        count = len(result_block["content"]) if isinstance(result_block["content"], list) else 0
                        # Server tool: bills flat per request, but its result
                        # block still injects bytes into the next-turn prompt
                        # as input. Record both — dashboard combines them.
                        try:
                            import aime.usage as _usage
                            _usage.record_tool_use(
                                self._usage_label,
                                "web_search",
                                "server",
                                model=turn_model,
                                result_bytes=len(json.dumps(result_block.get("content"))),
                                web_search_requests=1,
                                session_id=self._session_id,
                                source=self._usage_source,
                            )
                        except Exception:
                            pass
                        yield BackendEvent(
                            kind="assistant_use_tool",
                            tool_name="web_search_result",
                            tool_input={"results": count},
                            tool_use_id=block.tool_use_id,
                            expects_response=False,
                        )
                    elif block.type == "text":
                        current_text = []
                        text_stripper = _ClockTagStripper()
                elif etype == "content_block_delta":
                    delta = event.delta
                    dtype = getattr(delta, "type", None)
                    if dtype == "text_delta":
                        clean = text_stripper.feed(delta.text)
                        if clean:
                            current_text.append(clean)
                            yield BackendEvent(kind="assistant_text_delta", text=clean)
                    elif dtype == "input_json_delta":
                        partial_json += delta.partial_json
                elif etype == "content_block_stop":
                    if current_tool is not None:
                        try:
                            current_tool["input"] = (
                                json.loads(partial_json) if partial_json else {}
                            )
                        except json.JSONDecodeError:
                            current_tool["input"] = {}
                        is_server = current_tool["type"] == "server_tool_use"
                        with self._lock:
                            assistant_blocks.append(current_tool)
                            if not is_server:
                                # Register expectation before yielding so a fast
                                # tool_send_response can't see an empty set and
                                # prematurely flush pending results.
                                self._expected_tool_use_ids.add(current_tool["id"])
                                # Stash name + model so the matching
                                # tool_send_response can attribute the result's
                                # byte size to this tool, priced at this turn's
                                # model rate.
                                self._tool_attrib[current_tool["id"]] = (
                                    current_tool["name"], turn_model,
                                )
                        yield BackendEvent(
                            kind="assistant_use_tool",
                            tool_name=current_tool["name"],
                            tool_input=dict(current_tool["input"]),
                            tool_use_id=current_tool["id"],
                            expects_response=not is_server,
                        )
                        current_tool = None
                        partial_json = ""
                    else:
                        tail = text_stripper.flush()
                        if tail:
                            current_text.append(tail)
                            yield BackendEvent(kind="assistant_text_delta", text=tail)
                        if current_text:
                            text = "".join(current_text)
                            with self._lock:
                                assistant_blocks.append({"type": "text", "text": text})
                            yield BackendEvent(kind="assistant_text_end", text=text)
                            current_text = []

            if self._interrupted.is_set():
                # Mid-stream interrupt. Leave _messages in a state that's
                # both valid (alternating roles, no orphan tool_use/result)
                # and truthful (don't fabricate content the model didn't
                # produce). The SDK's final-message accessor is skipped —
                # the stream was aborted, calling it can raise.
                self._cleanup_interrupted_turn(assistant_blocks)
                self._interrupted.clear()
                with self._lock:
                    self._current_turn_model = None
                    self._current_turn_label = None
                yield BackendEvent(kind="turn_end", stop_reason="interrupted")
                return

            final = stream.get_final_message()
            stop_reason = final.stop_reason
            turn_ms = (datetime.datetime.now() - turn_started).total_seconds() * 1000.0
            self._record_usage(
                getattr(final, "usage", None),
                getattr(final, "model", None) or turn_model,
                purpose="turn",
                stop_reason=stop_reason,
                duration_ms=turn_ms,
                routed_decision=turn_label,
            )

        with self._lock:
            if not assistant_blocks:
                # Drop the empty placeholder so we don't send a bogus
                # assistant message back next turn.
                if self._messages and self._messages[-1].get("content") is assistant_blocks:
                    self._messages.pop()

        # Flush any tool_results buffered by submit() during the stream as
        # a single user message. Deferred until here so that all tool_use
        # blocks the model emitted have already landed in assistant_blocks
        # before we close the assistant turn — flushing eagerly inside
        # submit() would orphan later tool_use blocks (see comment in
        # submit()'s tool_send_response branch).
        flushed = False
        with self._lock:
            if self._pending_tool_results:
                self._messages.append({
                    "role": "user",
                    "content": self._pending_tool_results,
                })
                self._pending_tool_results = []
                flushed = True

        self._persist()

        if stop_reason != "tool_use":
            # Tool loop is over — drop the sticky pick so the next user turn
            # re-asks the router.
            with self._lock:
                self._current_turn_model = None
                self._current_turn_label = None
            # Kick off compaction *after* the turn has finished streaming so the
            # Haiku summary + title-refresh calls never delay the user-visible
            # response. The next turn pays full prefix cost only if compaction
            # hasn't landed yet; once it does, subsequent turns benefit.
            self._spawn_compaction()
            # If this turn's debit crossed a budget threshold, surface the
            # notice now (before turn_end) so the UI can show a gentle banner.
            # Stashed by _record_usage, which can't yield. Notify only — no turn
            # is blocked (the enforcement action is deferred; see aime.quota).
            notice = self._usage_notice_pending
            self._usage_notice_pending = None
            if notice is not None:
                yield BackendEvent(
                    kind="usage_notice",
                    text=notice["state"],
                    tool_result=notice["status"],
                )
            yield BackendEvent(kind="turn_end", stop_reason=stop_reason or "end_turn")
        elif flushed:
            # All tool_results are in and appended as one user message —
            # wake the outer stream() loop so it starts the next turn.
            self._turn_trigger.set()

    def _cacheable_messages(self, messages: list[dict]) -> list[dict]:
        """Return a shallow copy of `messages` with a prompt-cache breakpoint on
        the last content block of the final message. This caches the entire
        history prefix, so each agent-loop turn only pays full input price for
        blocks appended since the previous call. The dicts in self._messages are
        left untouched — the breakpoint lives only on the copy sent to the API.

        TTL is 5m (1.25x write premium), not 1h (2x). The message tail churns
        within a session and is read back within seconds-to-minutes inside the
        agent tool loop; a 1h write only pays off when a segment is reused
        >1.11x, whereas 5m breaks even at >0.28x. The stable system prompt and
        tool schemas keep their 1h TTL — only this growing tail is 5m.

        The volatile date/time block is appended *after* the breakpoint so it
        stays out of the cached prefix. Putting it inside the prefix (e.g. in
        the system array) busts the whole history cache on every minute change
        — which, with sub-5-minute turn gaps, means a near-total rewrite every
        fresh user turn."""
        if not messages:
            return messages
        # Slim every inline-graphic source down to a short, deterministic
        # placeholder (the model keeps each graphic's id + summary, and reloads
        # the source on demand via GetGraphic). Non-mutating: the full source
        # stays in self._messages for persistence, replay, and editing — only
        # this API-bound copy is stripped. Done before the cache breakpoint so
        # the slimmed tail is what gets cached.
        messages = _graphics.redact_history_graphics(messages)
        out = list(messages)
        last = out[-1]
        content = last.get("content")
        if isinstance(content, list) and content:
            new_content = list(content)
            new_content[-1] = {
                **new_content[-1],
                "cache_control": {"type": "ephemeral", "ttl": "5m"},
            }
            # Only attach the clock to fresh user-text turns. On tool_result
            # turns the trailing <clock> block becomes the sole "textual" thing
            # the model sees the user say, which makes it narrate as if the
            # user's message was empty (and tempts Haiku to echo the tag back).
            if last.get("role") == "user" and not any(
                isinstance(b, dict) and b.get("type") == "tool_result"
                for b in content
            ):
                new_content.append(self._date_block())
            out[-1] = {**last, "content": new_content}
        return out

    @classmethod
    def _is_summary_message(cls, msg: dict) -> bool:
        content = msg.get("content")
        return (
            msg.get("role") == "user"
            and isinstance(content, list)
            and bool(content)
            and isinstance(content[0], dict)
            and content[0].get("type") == "text"
            and content[0].get("text", "").startswith(cls._SUMMARY_MARKER)
        )

    def _cleanup_interrupted_turn(self, assistant_blocks: list[dict] | None) -> None:
        """Restore self._messages to an API-valid alternating shape after
        an interrupt, regardless of which sub-state we were in.

        `assistant_blocks` is the content list of the in-flight assistant
        message (passed from `_run_turn`), or None when called from the
        outer `stream()` loop between turns. The cleanup branches:

          * **Mid-stream, nothing appended after the assistant placeholder**
            (the common case): strip orphan tool_use blocks from
            `assistant_blocks`. Keep any text the model already streamed;
            otherwise substitute a `[interrupted]` text stub so the
            assistant slot isn't empty.
          * **Mid-stream, tool_result user message landed after the
            placeholder** (slow-tool race): the tool_use+result pair is
            valid history, so we leave both untouched and append a
            synthetic assistant `[interrupted]` stub to preserve
            alternation against the next user message.
          * **Between turns, last message is assistant with unresolved
            tool_use blocks** (waiting on tool_results): strip orphan
            tool_use blocks, replace with text/stub.
          * **Between turns, last message is user (tool_results)**: append
            synthetic assistant stub for alternation.

        Always clears `_pending_tool_results` and the relevant
        `_expected_tool_use_ids`. Persists the result.
        """
        with self._lock:
            # Discard any tool_results we haven't yet appended as a user
            # message — they belong to tool_use IDs we're about to orphan.
            self._pending_tool_results = []
            self._tool_attrib.clear()

            # Locate the assistant placeholder we appended at the start
            # of this turn (mid-stream cases). The reference comparison
            # is robust even if other messages have been appended after.
            placeholder_idx = -1
            if assistant_blocks is not None:
                for i, m in enumerate(self._messages):
                    if m.get("content") is assistant_blocks:
                        placeholder_idx = i
                        break

            if placeholder_idx == -1:
                # Between-turn case: no placeholder owned by this call.
                # Decide based on the last message's role.
                if not self._messages:
                    self._persist()
                    return
                last = self._messages[-1]
                last_role = last.get("role")
                if last_role == "assistant":
                    self._strip_to_text_or_stub(last)
                elif last_role == "user":
                    self._messages.append({
                        "role": "assistant",
                        "content": [{"type": "text", "text": "[interrupted]"}],
                    })
            else:
                # Mid-stream case. Check whether anything was appended
                # after the placeholder (e.g. a tool_result user message
                # from a fast tool that completed before the interrupt).
                if placeholder_idx == len(self._messages) - 1:
                    self._strip_to_text_or_stub(self._messages[placeholder_idx])
                else:
                    # Slow-tool race: keep the assistant + user(tool_result)
                    # pair intact and append a synthetic assistant stub
                    # so the next user message has a valid predecessor.
                    if self._messages[-1].get("role") == "user":
                        self._messages.append({
                            "role": "assistant",
                            "content": [{"type": "text", "text": "[interrupted]"}],
                        })
        self._persist()

    def _strip_to_text_or_stub(self, message: dict) -> None:
        """Remove tool_use / server_tool_use blocks from an assistant
        message's content. Keep any non-empty text blocks; if none remain,
        substitute a single `[interrupted]` text block so the assistant
        slot isn't empty (the API rejects messages with empty content).
        Caller must hold self._lock. Updates `_expected_tool_use_ids` to
        drop any IDs we just orphaned."""
        content = message.get("content")
        if not isinstance(content, list):
            return
        for blk in content:
            if isinstance(blk, dict) and blk.get("type") in ("tool_use", "server_tool_use"):
                self._expected_tool_use_ids.discard(blk.get("id"))
        kept = [
            b for b in content
            if isinstance(b, dict)
            and b.get("type") == "text"
            and (b.get("text") or "").strip()
        ]
        if not kept:
            kept = [{"type": "text", "text": "[interrupted]"}]
        message["content"] = kept

    def _generate_title(self, prompt_text: str) -> None:
        """Background: one cheap Haiku call turning the user's opening prompt
        into a one-sentence session description, then persist it. Best-effort —
        any failure just leaves the summary empty so the next submit() retries."""
        try:
            try:
                resp = self._client.messages.create(
                    model=self.COMPACT_MODEL,
                    system=(
                        "Generate a short title for this conversation based on the "
                        "user's message(s). Rules:\n"
                        "- 2-5 words maximum\n"
                        "- Noun phrase only — no \"User wants\", \"User asks\", or similar\n"
                        "- Capture the core subject, not the action\n"
                        "- Be specific, not generic (\"Flowers for Joanna\" not \"Date Planning\")\n"
                        "- If it's a test or trivial message, just say \"Test\"\n"
                        "- Ignore any bracketed [System info] or auto-injected context\n"
                        "- Do NOT answer the user's request — only title it\n"
                        "Return only the title, no punctuation, no quotes."
                    ),
                    messages=[{"role": "user", "content": "[Start users messages to ASSISTANT, NOT to you] " + prompt_text + "[End users messages to ASSISTANT, NOT to you]"}],
                    max_tokens=64,
                )
                self._record_usage(
                    getattr(resp, "usage", None), self.COMPACT_MODEL, purpose="title"
                )
                title = "".join(
                    b.text for b in resp.content if getattr(b, "type", None) == "text"
                ).strip()
            except Exception:
                return
            if title:
                with self._lock:
                    self._summary = title
                self._persist()
        finally:
            with self._lock:
                self._title_generating = False

    def _refresh_title(self, recent_user_texts: list[str]) -> None:
        """During compaction, let Haiku tighten or correct the session
        description based on the most recent user messages. The compaction
        summary is deliberately *not* used here — it leans on bootstrapped
        data and older history, so titles derived from it drift away from
        what the conversation is currently about. Updates self._summary in
        place; the caller persists. Best-effort."""
        if not recent_user_texts:
            return
        recent_block = "\n\n".join(
            f"- {t}" for t in recent_user_texts if t and t.strip()
        )
        if not recent_block:
            return
        try:
            resp = self._client.messages.create(
                model=self.COMPACT_MODEL,
                system=(
                    "You maintain a short title (2-5 words, noun phrase only) for "
                    "a conversation. Given the current title and the user's most "
                    "recent messages, return an updated title that reflects what "
                    "the conversation is *currently* about, or the original "
                    "unchanged if it is still accurate. Rules:\n"
                    "- 2-5 words maximum\n"
                    "- Noun phrase only — no \"User wants\", \"User asks\", or similar\n"
                    "- Capture the core subject, not the action\n"
                    "- Be specific, not generic\n"
                    "- Ignore any bracketed [System info] or auto-injected context\n"
                    "Return only the title, no punctuation, no quotes."
                ),
                messages=[{
                    "role": "user",
                    "content": (
                        f"Current description:\n{self._summary or '(none yet)'}\n\n"
                        f"Most recent user messages:\n{recent_block}"
                    ),
                }],
                max_tokens=64,
            )
            self._record_usage(
                getattr(resp, "usage", None), self.COMPACT_MODEL, purpose="title"
            )
            title = "".join(
                b.text for b in resp.content if getattr(b, "type", None) == "text"
            ).strip()
        except Exception:
            return
        if title:
            with self._lock:
                self._summary = title

    def _summarize(self, old: list[dict], prev_summary: str) -> str:
        """One cheap Haiku call: condense `old` into a dense factual summary,
        merging `prev_summary` if a prior compaction left one. Raises on API
        failure (the caller treats that as "skip compaction this turn")."""
        instructions = (
            "You are a conversation summarizer for AiMe, a personal assistant app.\n\n"
            "Your job is to compress the conversation history into a compact summary "
            "that preserves everything a future AI session would need to continue "
            "seamlessly.\n\n"
            "Include:\n"
            "- Decisions made\n"
            "- Actions taken (events created/edited/archived, topics updated — include IDs)\n"
            "- New information shared by the user\n"
            "- Unresolved threads or open questions from the conversation\n"
            "- Any instructions or preferences the user expressed\n\n"
            "Do NOT include:\n"
            "- The contents of the \"About Me\" or \"Pending\" topics — these are "
            "automatically injected at the start of every session fresh from the "
            "database. Summarizing them here is redundant and wastes context.\n"
            "- General background about the user that belongs in a topic — only "
            "include it if it was newly shared this conversation and may not yet "
            "be saved.\n\n"
            "Keep the summary dense and factual. Use bullet points and headers. "
            "Aim for brevity — only include what a future session couldn't recover "
            "from topics and events alone."
        )
        parts = []
        if prev_summary:
            parts.append("Existing summary so far:\n" + prev_summary)
        parts.append(
            "New messages to fold in (JSON):\n"
            + json.dumps(old, ensure_ascii=False)
        )
        parts.append("Return the updated combined summary.")
        resp = self._client.messages.create(
            model=self.COMPACT_MODEL,
            system=instructions,
            messages=[{"role": "user", "content": "\n\n".join(parts)}],
            max_tokens=2048,
        )
        self._record_usage(
            getattr(resp, "usage", None), self.COMPACT_MODEL, purpose="compaction"
        )
        return "".join(
            b.text for b in resp.content if getattr(b, "type", None) == "text"
        ).strip()

    def _spawn_compaction(self) -> None:
        """Run history compaction on a daemon thread. Bails immediately if a
        compaction is already in flight or if the history isn't long enough to
        compact yet — both checks are cheap and keep the turn loop fast."""
        with self._lock:
            if self._compacting:
                return
            if len(self._messages) <= self.COMPACT_TRIGGER_MSGS:
                return
            self._compacting = True
        threading.Thread(target=self._run_compaction_bg, daemon=True).start()

    def _run_compaction_bg(self) -> None:
        try:
            with self._lock:
                pre_compact = list(self._messages)
            compacted = self._maybe_compact(pre_compact)
            if compacted is not pre_compact:
                with self._lock:
                    # Any messages appended while compaction was running (tool
                    # results, a follow-up user turn) sit past pre_compact's
                    # length and are spliced back on verbatim.
                    tail = self._messages[len(pre_compact):]
                    self._messages = compacted + tail
                self._persist()
        finally:
            with self._lock:
                self._compacting = False

    def _maybe_compact(self, messages: list[dict]) -> list[dict]:
        """If `messages` has grown past COMPACT_TRIGGER_MSGS, fold the oldest
        ~COMPACT_BATCH_MSGS messages (merging any prior summary) into a single
        summary message and return the shortened list. Returns the *same list
        object* unchanged when compaction isn't needed or can't be done safely
        — callers use identity (`is`) to detect whether anything happened.

        The cut point is advanced from COMPACT_BATCH_MSGS to the next assistant
        message so the verbatim tail starts with an assistant turn (keeping the
        [summary(user), assistant, ...] alternation the API expects) and no
        tool_use/tool_result pair is split across the boundary."""
        if len(messages) <= self.COMPACT_TRIGGER_MSGS:
            return messages

        cut = self.COMPACT_BATCH_MSGS
        while cut < len(messages) and messages[cut].get("role") != "assistant":
            cut += 1
        # Bail if there's no safe cut or it would leave a trivial tail.
        if cut >= len(messages) - 2:
            return messages

        old, tail = messages[:cut], messages[cut:]
        prev_summary = ""
        if old and self._is_summary_message(old[0]):
            prev_summary = old[0]["content"][0]["text"]

        try:
            summary_text = self._summarize(old, prev_summary)
        except Exception:
            # Summarization failed — keep the full history rather than lose it.
            return messages
        if not summary_text:
            return messages

        # Compaction is infrequent and already runs off-lock, so spend one more
        # cheap Haiku call here to keep the session description current. Derive
        # the title from the last few user messages in the verbatim tail, not
        # from the compaction summary — the summary skews toward bootstrapped
        # context and older history, which produces stale-feeling titles.
        recent_user_texts: list[str] = []
        for msg in reversed(tail):
            if msg.get("role") != "user":
                continue
            content = msg.get("content")
            if not isinstance(content, list):
                continue
            texts = [
                b.get("text", "")
                for b in content
                if isinstance(b, dict) and b.get("type") == "text"
            ]
            joined = "\n".join(t for t in texts if t)
            if joined:
                recent_user_texts.append(joined)
            if len(recent_user_texts) >= 3:
                break
        recent_user_texts.reverse()
        self._refresh_title(recent_user_texts)

        summary_msg = {
            "role": "user",
            "content": [{
                "type": "text",
                "text": f"{self._SUMMARY_MARKER}\n{summary_text}",
            }],
        }
        return [summary_msg, *tail]

    def _load_schema(self, schema_path: str) -> dict:
        with open(schema_path, "r") as f:
            return self._schema_to_tool(json.load(f))

    @staticmethod
    def _schema_to_tool(schema: dict) -> dict:
        """Translate a JSON-schema tool definition ({title, description, type,
        properties, [required]}) into the Anthropic tool shape ({name,
        description, input_schema}). Shared by the file loader and by callers
        that build a schema in memory (e.g. a per-agent SubmitResult)."""
        input_schema = {"type": schema["type"], "properties": schema["properties"]}
        if "required" in schema:
            input_schema["required"] = schema["required"]
        return {
            "name": schema["title"],
            "description": schema["description"],
            "input_schema": input_schema,
        }

    @staticmethod
    def _last_user_has_images(messages: list[dict]) -> bool:
        """True when the most recent user message carries an image block.
        The router uses this as a "force Sonnet" signal — image-bearing turns
        are almost never read-only lookups in this app."""
        for msg in reversed(messages):
            if msg.get("role") != "user":
                continue
            content = msg.get("content")
            if not isinstance(content, list):
                return False
            return any(
                isinstance(b, dict) and b.get("type") == "image"
                for b in content
            )
        return False

    def _record_usage(
        self,
        usage,
        model: str,
        purpose: str,
        *,
        stop_reason: str | None = None,
        duration_ms: float | None = None,
        routed_decision: str | None = None,
    ) -> None:
        """Hand one API call's token usage to the opt-in usage log. Imported
        lazily and fully guarded — usage accounting must never break a turn.

        `self._session_id` is passed through so a report can group cost by
        conversation; aime.usage decides whether to actually persist it based
        on the AIME_USAGE_LINK_USERS opt-in."""
        try:
            import aime.usage as _usage
            _usage.record_api(
                self._usage_label, model, usage, purpose=purpose,
                session_id=self._session_id,
                stop_reason=stop_reason,
                duration_ms=duration_ms,
                routed_decision=routed_decision,
                source=self._usage_source,
            )
        except Exception:
            # Usage logging is opt-in and best-effort; it must never break a
            # turn. Log at debug so a persistent failure is discoverable without
            # spamming a deployment that simply has the log disabled.
            logger.debug("usage log record failed", exc_info=True)
        # Debit this call's real cost from the user's budget (independent of the
        # opt-in usage log above). Fully guarded — a quota failure must never
        # break a turn, so it fails open (the turn proceeds). Captures every
        # purpose (turn/title/compaction/route) since they all flow through here.
        if self._quota is not None:
            try:
                from aime import quota as _quota
                from aime import pricing as _pricing
                cost = _pricing.cost_from_usage(model, usage)
                decision = self._quota.debit(cost)
                # Surface a banner only on a *transition* into a notify state, so
                # the user isn't nagged every turn. Stashed for _run_turn to emit
                # (this method can't yield). Background-purpose calls (title/
                # compaction) still debit but never set a pending UI notice.
                if (decision in (_quota.Decision.NOTIFY_LOW, _quota.Decision.OVER)
                        and decision != self._last_usage_decision
                        and purpose == "turn"):
                    self._usage_notice_pending = {
                        "state": decision.value,
                        "status": self._quota.status(),
                    }
                self._last_usage_decision = decision
            except Exception:
                # The budget debit fails open: a quota bug must never break a
                # turn. But a *persistent* failure here silently stops charging
                # everyone (enforcement quietly disabled), so log at warning —
                # this is the signal that the cost-control ledger is broken.
                logger.warning("quota debit failed; turn not charged",
                               exc_info=True)


class SessionsBackend:
    """Anthropic Agents/Sessions beta implementation of AgentBackend.

    Owns a single live session at a time. Caches the (environment, agent)
    pair on disk keyed by a hash of the system prompt + model + tool schemas
    so identical configurations reuse the same agent across restarts.
    """

    def __init__(
        self,
        system_prompt: str,
        model: str,
        schema_files: list[str],
        config_path: str,
    ):
        self._client = Anthropic(max_retries=3)
        self._system_prompt = system_prompt
        self._model = model
        self._schema_files = schema_files
        self._config_path = config_path
        self._env_id: str | None = None
        self._agent_id: str | None = None
        self._agent_version: str | None = None
        self._session = None

    # --- AgentBackend interface ---

    def new_session(self) -> str:
        self._env_id, self._agent_id, self._agent_version = self._get_or_create_setup()
        try:
            self._session = self._client.beta.sessions.create(
                agent={"type": "agent", "id": self._agent_id, "version": self._agent_version},
                environment_id=self._env_id,
            )
        except Exception:
            # Stale cached agent/env — recreate from scratch.
            if os.path.exists(self._config_path):
                os.remove(self._config_path)
            self._env_id, self._agent_id, self._agent_version = self._get_or_create_setup()
            self._session = self._client.beta.sessions.create(
                agent={"type": "agent", "id": self._agent_id, "version": self._agent_version},
                environment_id=self._env_id,
            )
        return self._session.id

    def load_session(self, session_id: str) -> None:
        # Sessions are server-side; "loading" just means attaching to the id.
        # The streaming endpoint will pick up wherever the session left off.
        self._session = type("Session", (), {"id": session_id})()

    def list_sessions(self) -> list[SessionInfo]:
        # Sessions are server-side and not enumerated locally; nothing to list.
        return []

    def set_session_context(self, text: str) -> None:
        # Beta Sessions backend manages its own server-side state; nothing to
        # attach client-side. Kept to satisfy the AgentBackend protocol.
        return

    def set_client_timezone(self, tz: str) -> None:
        # Date/time is handled server-side by the Beta Sessions agent; nothing
        # to inject client-side. Kept to satisfy the AgentBackend protocol.
        return

    def messages_snapshot(self) -> list[dict]:
        # Server-side history isn't mirrored client-side here.
        return []

    def reset(self) -> None:
        if self._session is not None:
            try:
                self._client.beta.sessions.terminate(session_id=self._session.id)
            except Exception:
                pass
        self.new_session()

    def delete_session(self, session_id: str) -> None:
        # Server-side sessions; nothing to delete locally.
        return

    def delete_all_sessions(self) -> None:
        return

    def shutdown(self) -> None:
        if self._session is not None:
            try:
                self._client.beta.sessions.terminate(session_id=self._session.id)
            except Exception:
                pass

    def submit(self, event: BackendEvent) -> None:
        if self._session is None:
            raise RuntimeError("no active session; call new_session() first")

        if event.kind in ("user_send_message", "system_send_message"):
            self._client.beta.sessions.events.send(
                session_id=self._session.id,
                events=[{
                    "type": "user.message",
                    "content": [{"type": "text", "text": event.text or ""}],
                }],
            )
        elif event.kind == "tool_send_response":
            self._client.beta.sessions.events.send(
                session_id=self._session.id,
                events=[{
                    "type": "user.custom_tool_result",
                    "custom_tool_use_id": event.tool_use_id,
                    "content": [{"type": "text",
                                 "text": json.dumps(event.tool_result)}],
                }],
            )
        else:
            raise ValueError(f"submit() does not accept event kind: {event.kind}")

    def stream(self) -> Iterator[BackendEvent]:
        if self._session is None:
            raise RuntimeError("no active session; call new_session() first")
        with self._client.beta.sessions.events.stream(
            session_id=self._session.id
        ) as stream:
            for raw in stream:
                for normalized in self._normalize(raw):
                    yield normalized
                if raw.type == "session.status_terminated":
                    return

    # --- internal: provider-specific normalization ---

    def _normalize(self, event) -> Iterator[BackendEvent]:
        if event.type == "agent.message":
            for block in event.content:
                if block.type == "text":
                    yield BackendEvent(kind="assistant_send_text", text=block.text)
        # Thinking currently dissabled.
        #elif event.type == "agent.thinking":
        #    for block in event.content:
        #        if block.type == "text":
        #            yield BackendEvent(kind="assistant_thinking", text=block.text)
        elif event.type == "agent.custom_tool_use":
            inp = dict(event.input) if event.input else {}
            yield BackendEvent(
                kind="assistant_use_tool",
                tool_name=event.name,
                tool_input=inp,
                tool_use_id=event.id,
                expects_response=True,
            )
        elif event.type == "agent.tool_use":
            inp = dict(event.input) if getattr(event, "input", None) else {}
            yield BackendEvent(
                kind="assistant_use_tool",
                tool_name=event.name,
                tool_input=inp,
                tool_use_id=getattr(event, "id", None),
                expects_response=False,
            )
        elif event.type == "session.status_idle":
            yield BackendEvent(
                kind="turn_end",
                stop_reason=getattr(event.stop_reason, "type", None),
            )
        elif event.type == "session.status_terminated":
            yield BackendEvent(kind="session_terminated")

    # --- internal: setup / caching ---

    def _setup_hash(self) -> str:
        h = hashlib.sha256()
        h.update(self._system_prompt.encode())
        h.update(self._model.encode())
        for path in self._schema_files:
            with open(path, "rb") as f:
                h.update(f.read())
        return h.hexdigest()

    def _load_schema(self, schema_path: str) -> dict:
        with open(schema_path, "r") as f:
            schema = json.load(f)
        input_schema = {"type": schema["type"], "properties": schema["properties"]}
        if "required" in schema:
            input_schema["required"] = schema["required"]
        return {
            "type": "custom",
            "name": schema["title"],
            "description": schema["description"],
            "input_schema": input_schema,
        }

    def _create_environment_and_agent(self):
        environment = self._client.beta.environments.create(
            name="calendar-env",
            config={"type": "cloud", "networking": {"type": "unrestricted"}},
        )
        agent = self._client.beta.agents.create(
            name="Assistant",
            model=self._model,
            system=self._system_prompt,
            tools=[
                {"type": "agent_toolset_20260401"},
                *[self._load_schema(p) for p in self._schema_files],
            ],
        )
        return environment.id, agent.id, agent.version

    def _get_or_create_setup(self):
        current_hash = self._setup_hash()
        if not os.environ.get("FORCE_RECREATE") and os.path.exists(self._config_path):
            try:
                with open(self._config_path) as f:
                    cfg = json.load(f)
                if cfg.get("hash") == current_hash:
                    return cfg["environment_id"], cfg["agent_id"], cfg["agent_version"]
            except (json.JSONDecodeError, KeyError):
                pass
        env_id, agent_id, agent_version = self._create_environment_and_agent()
        with open(self._config_path, "w") as f:
            json.dump(
                {
                    "environment_id": env_id,
                    "agent_id": agent_id,
                    "agent_version": agent_version,
                    "hash": current_hash,
                },
                f,
                indent=2,
            )
        return env_id, agent_id, agent_version
