"""Conversation controller — the brain of Aime, UI-agnostic.

Owns the active `AgentBackend` session, drives the agent loop, executes tools
via the local `ToolGateway`, and emits a high-level `CoreEvent` stream to one
or more subscribers. Any frontend (Textual TUI, CLI, web) attaches by:

    controller.subscribe(callback)
    controller.start()
    ...
    controller.dispatch_input("...")

Threading: `start()` and `reset()` and `load()` each spawn a fresh stream
worker via the `worker_spawner` callable injected at construction time. The
worker calls `run_stream_loop()` which blocks on `backend.stream()` until the
session terminates. Subscribers are invoked on whatever thread emits the
event (worker thread for agent output; calling thread for input handlers);
frontends are responsible for thread-marshaling if their UI toolkit needs it.
"""

import json
import threading
from dataclasses import dataclass, field
from typing import Callable, Literal

from provider_backend import AgentBackend, BackendEvent, SessionInfo

from .tool_gateway import ToolGateway
from .commitments import CommitmentService
from .services import _events_from
from .tool_formatting import (
    format_tool_details,
    format_tool_response,
    format_tool_result_for_model,
)
from .onboarding import (
    bootstrap_special_topics,
    should_run_onboarding,
    OnboardingState,
    ONBOARDING_PROMPT,
)


# Cap on instances per commitment when auto-attaching history to a
# FilterUsersEvents result — keeps the enrichment bounded for commitments with
# long histories. The model can still call GetCommitmentHistory for the full set.
_AUTO_HISTORY_LIMIT = 10


CoreEventKind = Literal[
    "user_message_shown",       # user msg accepted and sent to backend
    "user_message_queued",      # user msg queued (backend was busy)
    "assistant_text",           # complete text block (non-streaming providers)
    "assistant_text_delta",     # streaming text chunk
    "assistant_text_end",       # end of a streamed text block
    "assistant_thinking",       # model thinking trace (only when logging enabled)
    "tool_call",                # tool invocation started
    "tool_result",              # tool invocation finished
    "turn_end",                 # one agent turn finished (text or stop_reason)
    "ready",                    # idle, awaiting user input
    "notice",                   # status banner (info/warning/error/success)
    "session_restart",          # transcript should be cleared (reset or load)
    "session_terminated",       # backend session closed
    "error",                    # unrecoverable controller/backend error
    "turn_routing",             # router picked a model for the next turn
                                # (emitted only when verbose mode is on)
    "agent_result",             # headless background agent called SubmitResult;
                                # carries the structured result in `payload`
]


Severity = Literal[
    "info", "warning", "error", "success", "recovery", "loaded", "new_session",
    # Signal-only severities (no visible banner): tell the frontend that the
    # first-run onboarding flow has started / finished so it can show or hide
    # onboarding-only affordances like the empty-state upload nudge.
    "onboarding", "onboarding_done",
]
RestartReason = Literal["reset", "load"]


@dataclass
class CoreEvent:
    kind: CoreEventKind
    text: str = ""
    tool_name: str = ""
    tool_details: str = ""
    tool_result_summary: str = ""
    # The complete, multi-line detail behind the one-line summaries above: the
    # full tool input on a `tool_call`, the full result on a `tool_result`. A
    # verbose-mode frontend can show it in a collapsible dropdown; quieter
    # tiers ignore it. Empty when there's nothing more than the summary.
    tool_detail_full: str = ""
    severity: Severity = "info"
    restart_reason: RestartReason | None = None
    stop_reason: str = ""
    # Set on events emitted during /load history replay. Lets the frontend
    # skip live-only affordances like the "thinking…" placeholder.
    from_replay: bool = False
    # User-message attachments (images, embedded text files). Each entry is
    # {"kind": "image", "media_type": str, "data": str (base64)} for images.
    # Text files are still embedded in `text` via <aime:file> sentinels; the
    # frontend extracts and renders them alongside these.
    attachments: list[dict] = field(default_factory=list)
    # Structured payload for events that carry one. Currently only set on
    # `agent_result`, where it holds the SubmitResult tool input
    # ({"summary": str, "result": ...}). None for ordinary events.
    payload: dict | None = None


Subscriber = Callable[[CoreEvent], None]
WorkerSpawner = Callable[[Callable[[], None]], None]


def _full_detail_text(value) -> str:
    """The complete, human-readable detail behind a tool's one-line summary —
    the full input args or the full result — for a verbose-mode dropdown.

    Strings (e.g. the WebSearch digest) pass through; dicts/lists are
    pretty-printed JSON. Best-effort: an unserializable value falls back to its
    repr rather than raising, since this only feeds an optional UI affordance.
    """
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, indent=2, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(value)


class ConversationController:
    def __init__(
        self,
        backend: AgentBackend,
        tool_gateway: ToolGateway,
        worker_spawner: WorkerSpawner,
        web_search_agent=None,
        headless: bool = False,
        messenger=None,
        message_recipient: str | None = None,
        reminder_service=None,
    ):
        self._backend = backend
        self._tools = tool_gateway
        self._spawn_worker = worker_spawner
        # Optional event-reminder engine (aime.scheduling.ReminderService). When
        # set, the CreateReminder / ListReminders / DeleteReminder client tools
        # are handled here against the user's ScheduleStore; when None they
        # degrade gracefully (a "not available" result), so a backend that
        # doesn't offer the schemas — or a misfire — can never reach the gateway.
        self._reminder_service = reminder_service
        # The user's IANA timezone, set per session via set_client_timezone, so a
        # model-created reminder fires in their local time. None until the first
        # turn carries it (the frontend sends it on every /send).
        self._client_tz: str | None = None
        # Optional outbound messaging (see aime.messaging). `messenger` is a
        # MessageChannel; `message_recipient` is this user's opaque destination
        # (UserRecord.messaging_contact). When either is missing, SendMessage
        # and the SubmitResult `message_to_user` field degrade gracefully — the
        # tool returns a friendly "not set up" result and the field is ignored.
        self._messenger = messenger
        self._message_recipient = (message_recipient or "").strip() or None
        # Headless mode drives a background-agent run rather than an interactive
        # chat: start() skips the onboarding bootstrap and instead arms the
        # SubmitResult terminal tool, and the SubmitResult call is surfaced as
        # an `agent_result` CoreEvent (see _handle_tool_use). The full tool
        # dispatch path is otherwise identical, so background agents inherit
        # web search, commitment tools, and result formatting unchanged.
        self._headless = headless
        # Commitment-pattern tools (GetCommitmentHistory / GetPatternSummary /
        # GetRecentActivity) are computed in Python over `get_events` rather than
        # forwarded to the backend; handled in _handle_tool_use like WebSearch.
        self._commitments = CommitmentService(tool_gateway)
        # Optional Haiku-backed web search. When set, the `WebSearch` tool is
        # executed here (not via the HTTP gateway) by handing the request to
        # the sub-agent; see _handle_tool_use.
        self._web_search_agent = web_search_agent
        self._subscribers: list[Subscriber] = []
        # Conversation-level state. Presentation flags (e.g. whether the
        # "thinking…" line is visible) live in the frontend, not here.
        self._is_idle = True
        # Mirrors _is_idle as a threading.Event so stop_model() can block
        # until the in-flight turn has actually ended (so a follow-up
        # /send POSTed by the frontend is dispatched as the next turn
        # rather than landing in the queue during the gap and being lost).
        self._idle_event = threading.Event()
        self._idle_event.set()
        self._pending_user_messages: list[str] = []
        self._user_first_interaction = True
        self._log_model_thinking = False
        # Onboarding: a persisted per-user flag is the source of truth (see
        # OnboardingState). While onboarding is in flight we offer the model the
        # CompleteOnboarding tool; the model calls it after its closing message,
        # which is what marks onboarding done — see _handle_tool_use.
        self._onboarding = OnboardingState(
            getattr(self._backend, "conversations_dir", None)
        )

    # --- subscription ---

    def subscribe(self, callback: Subscriber) -> None:
        self._subscribers.append(callback)

    def _emit(self, event: CoreEvent) -> None:
        for sub in self._subscribers:
            sub(event)

    # --- lifecycle ---

    def start(self) -> None:
        """Spawn the stream worker and, for brand-new users, kick off the
        onboarding flow. Idempotent in the sense that calling it more than
        once just spawns an extra worker (which the backend's epoch logic
        will retire when sessions change), but it's intended to be called
        once at app boot."""
        self._spawn_worker(self.run_stream_loop)
        if self._headless:
            # Background-agent run: no onboarding. Arm SubmitResult for the
            # whole run so the worker can deliver its result and terminate.
            # The kickoff task message is submitted by the runner, not here.
            self._set_terminal_tool(True)
            return
        self._maybe_start_onboarding()

    def _maybe_start_onboarding(self) -> None:
        # Called on every entry into a fresh/empty conversation (app start and
        # /reset). Re-fires until the user has actually engaged, so a user who
        # saw the greeting but never replied gets it again instead of landing
        # in a blank chat — the bug that hit every beta tester.
        if not should_run_onboarding(self._onboarding, self._tools):
            return
        bootstrap = bootstrap_special_topics(self._tools)
        if bootstrap:
            self._backend.set_session_context(bootstrap)
        # bootstrap already ran — don't repeat on first user message
        self._user_first_interaction = False
        # Signal-only notice so the frontend knows onboarding is live (used to
        # show onboarding-only UI like the empty-state upload nudge). Cleared by
        # the matching "onboarding_done" notice when CompleteOnboarding fires.
        self._emit(CoreEvent(kind="notice", severity="onboarding"))
        # Offer the CompleteOnboarding tool for the duration of the flow.
        self._set_terminal_tool(True)
        try:
            self._backend.submit(BackendEvent(
                kind="system_send_message", text=ONBOARDING_PROMPT
            ))
            self._is_idle = False
        except Exception as exc:
            self._emit(CoreEvent(kind="error", text=f"onboarding send failed: {exc}"))

    def _set_terminal_tool(self, active: bool) -> None:
        """Toggle the model-facing terminal tool (CompleteOnboarding in an
        interactive session, SubmitResult in a headless agent run). Guarded so
        backends that don't support it (e.g. the legacy Sessions backend)
        degrade to the flag/backfill path rather than erroring."""
        setter = getattr(self._backend, "set_terminal_tool_active", None)
        if setter is not None:
            setter(active)

    def shutdown(self) -> None:
        try:
            self._backend.shutdown()
        except Exception:
            pass

    # --- input ---

    def dispatch_input(
        self,
        raw: str,
        images: list[dict] | None = None,
        *,
        hidden_prefix: str = "",
    ) -> bool:
        """Process a line of user input (slash commands or plain text).
        Optional `images` are forwarded to the backend with the next user
        message; ignored for slash commands. `hidden_prefix` is prepended to
        the text sent to the model but NOT shown in the user_message_shown
        event — used for out-of-band context (e.g. <stale> markers) that the
        model should see but the user shouldn't have echoed back in their own
        chat bubble. Returns True if the frontend should quit the app."""
        text = (raw or "").strip()
        if not text and not images:
            return False
        if text == ":q":
            return True
        if text == "/reset":
            self.reset()
            return False
        if text.startswith("/load"):
            parts = text.split(maxsplit=1)
            if len(parts) != 2 or not parts[1].strip():
                self._emit(CoreEvent(
                    kind="notice",
                    severity="warning",
                    text="Usage: /load <session-id> "
                         "(tab-complete to pick a saved conversation)",
                ))
                return False
            self.load(parts[1].strip())
            return False
        if text == "/toggle_log_model_thinking":
            self._log_model_thinking = not self._log_model_thinking
            self._emit(CoreEvent(
                kind="notice",
                severity="info",
                text=f"Log model thinking set to: {self._log_model_thinking}",
            ))
            return False
        self.send_user_message(text, images=images, hidden_prefix=hidden_prefix)
        return False

    def send_user_message(
        self,
        text: str,
        images: list[dict] | None = None,
        *,
        hidden_prefix: str = "",
    ) -> None:
        """Send (or queue) a plain user message without slash parsing."""
        if not self._is_idle:
            self._pending_user_messages.append((text, images))
            self._emit(CoreEvent(kind="user_message_queued", text=text))
            return
        self._dispatch_user_message(text, images=images, hidden_prefix=hidden_prefix)

    def _dispatch_user_message(
        self,
        text: str,
        images: list[dict] | None = None,
        *,
        hidden_prefix: str = "",
    ) -> None:
        attachments: list[dict] = []
        for img in (images or []):
            mt = img.get("media_type")
            data = img.get("data")
            if mt and data:
                attachments.append({
                    "kind": "image", "media_type": mt, "data": data,
                })
        self._emit(CoreEvent(
            kind="user_message_shown", text=text, attachments=attachments,
        ))
        if self._user_first_interaction:
            bootstrap = bootstrap_special_topics(self._tools)
            if bootstrap:
                self._backend.set_session_context(bootstrap)
            self._user_first_interaction = False
        # The prefix carries out-of-band context (e.g. <stale>…</stale>) that
        # the model should see but the user shouldn't — user_message_shown
        # above used the raw text, so the chat bubble doesn't include this.
        backend_text = (hidden_prefix + "\n" + text) if hidden_prefix else text
        try:
            self._backend.submit(BackendEvent(
                kind="user_send_message", text=backend_text, images=images,
            ))
            self._is_idle = False
            self._idle_event.clear()
        except Exception as exc:
            self._emit(CoreEvent(kind="error", text=f"send failed: {exc}"))

    def set_client_timezone(self, tz: str) -> None:
        """Forward the client's IANA timezone to the backend so per-turn
        timestamps reflect the user's local time rather than the server's, and
        to the tool gateway so events reads carry a user-local 'now' (used to
        reconcile stale past events). Also kept here so a reminder the model
        creates is interpreted in the user's timezone, not the server's."""
        self._client_tz = (tz or "").strip() or None
        self._backend.set_client_timezone(tz)
        self._tools.set_client_timezone(tz)

    def set_client_date_prefs(
        self, date_format: str | None, time_format: str | None
    ) -> None:
        """Forward the user's date/time *display* preferences to the backend so
        the per-turn clock block tells the model which format to write dates and
        times in (prose, event/topic summaries, messages). Display-only — the
        stored DD/MM/YYYY + HH:MM wire format is untouched."""
        self._backend.set_client_date_prefs(date_format, time_format)

    # --- session operations ---

    def stop_model(self, timeout: float = 5.0) -> bool:
        """Halt whatever the model is doing right now — generating a reply,
        running a tool, or waiting between tool calls — and block until the
        turn has fully ended.

        This is the single stop primitive. `/interrupt`, `reset()` (new
        conversation) and `load()` (switching conversations) all funnel
        through it, so the model never keeps replying into a conversation the
        user has already moved on from. The synchronicity matters: when this
        returns, `_is_idle` is True, so a follow-up `/send` dispatches
        immediately instead of racing into `_pending_user_messages` and being
        discarded when turn_end fires.

        Returns True if the controller became idle within the timeout, False
        if the deadline expired (a stuck model stream). reset()/load() ignore
        the result and swap the session out regardless; `/interrupt` surfaces
        it. No-op if already idle.
        """
        if self._idle_event.is_set():
            return True
        self._backend.interrupt_turn()
        return self._idle_event.wait(timeout=timeout)

    def reset(self) -> None:
        # Stop any in-flight turn first so the old conversation's reply can't
        # bleed events into the fresh one.
        self.stop_model()
        self._backend.reset()
        self._reset_internal_state()
        # session_restart = "clear the transcript / wipe presentation state".
        # The banner text follows separately as a notice so the frontend can
        # render it after the clear.
        self._emit(CoreEvent(kind="session_restart", restart_reason="reset"))
        # "new_session" severity (not "success") so the frontend renders it
        # as a centered Aime-logo welcome splash rather than a corner notice.
        self._emit(CoreEvent(
            kind="notice",
            severity="new_session",
            text="New conversation started",
        ))
        self._spawn_worker(self.run_stream_loop)
        # A returning user who never finished onboarding gets it again here,
        # rather than a blank new chat. No-op (and cheap) once they've engaged.
        self._maybe_start_onboarding()

    def load(self, session_id: str) -> None:
        # Switching conversations stops the model mid-turn — see stop_model().
        self.stop_model()
        try:
            self._backend.load_session(session_id)
        except (OSError, ValueError) as exc:
            self._emit(CoreEvent(
                kind="notice",
                severity="error",
                text=f"Could not load conversation '{session_id}': {exc}",
            ))
            return
        self._reset_internal_state()
        self._emit(CoreEvent(kind="session_restart", restart_reason="load"))
        # Replay deferred to keep the import graph shallow — replay imports
        # from controller for the CoreEvent type.
        from .replay import replay_messages
        for event in replay_messages(self._backend.messages_snapshot()):
            self._emit(event)
        title = ""
        for info in self._backend.list_sessions():
            if info.id == session_id:
                title = (info.summary or "").strip()
                break
        if title:
            preview = title if len(title) <= 40 else title[:40].rstrip() + "..."
            loaded_text = (
                f'Loaded conversation "{preview}". Continue where you left off.'
            )
        else:
            loaded_text = "Loaded conversation. Continue where you left off."
        # "loaded" severity (not "success") so the frontend can center it and
        # fade it away once the user sends a message — see web_chat.html.
        self._emit(CoreEvent(
            kind="notice",
            severity="loaded",
            text=loaded_text,
        ))
        self._spawn_worker(self.run_stream_loop)

    def _reset_internal_state(self) -> None:
        self._user_first_interaction = True
        # Withdraw the onboarding tool when leaving a conversation (reset/load).
        # reset() re-arms it via _maybe_start_onboarding if onboarding is due.
        self._set_terminal_tool(False)
        self._is_idle = True
        self._idle_event.set()
        self._pending_user_messages = []

    # --- queries used by frontends (e.g. autocomplete) ---

    @property
    def is_idle(self) -> bool:
        """True when no assistant turn is in flight. A frontend that missed
        the live `turn_end`/`ready` events (e.g. one that just connected and
        replayed history) can read this to recover the real busy state."""
        return self._is_idle

    def list_sessions(self) -> list[SessionInfo]:
        return self._backend.list_sessions()

    def delete_session(self, session_id: str) -> None:
        was_active = (
            getattr(self._backend, "_session_id", None) == session_id
        )
        self._backend.delete_session(session_id)
        if was_active:
            self.reset()

    def delete_all_sessions(self) -> None:
        self._backend.delete_all_sessions()
        self.reset()

    # --- stream worker ---

    def run_stream_loop(self) -> None:
        """Block on the backend's event stream, translating each backend
        event into one or more `CoreEvent`s. Returns when the backend signals
        `session_terminated`."""
        try:
            for event in self._backend.stream():
                self._handle_backend_event(event)
                if event.kind == "session_terminated":
                    return
        except Exception as exc:
            self._emit(CoreEvent(kind="error", text=f"stream error: {exc}"))

    def _handle_backend_event(self, event: BackendEvent) -> None:
        kind = event.kind
        if kind == "assistant_send_text":
            self._emit(CoreEvent(kind="assistant_text", text=event.text or ""))
        elif kind == "assistant_text_delta":
            self._emit(CoreEvent(kind="assistant_text_delta", text=event.text or ""))
        elif kind == "assistant_text_end":
            self._emit(CoreEvent(kind="assistant_text_end", text=event.text or ""))
        elif kind == "assistant_thinking":
            if self._log_model_thinking:
                self._emit(CoreEvent(kind="assistant_thinking", text=event.text or ""))
        elif kind == "assistant_use_tool":
            self._handle_tool_use(event)
        elif kind == "turn_routing":
            # Always forward the router's pick to frontends; whether it
            # actually surfaces in the UI is the frontend's verbosity
            # decision. The web frontend gates this on its `verbosity ===
            # "verbose"` setting (see web_chat.html); the TUI can do
            # similarly. Keeping the gate frontend-side avoids a
            # backend/frontend toggle pair that disagree.
            label = (event.text or "").strip() or "sonnet"
            self._emit(CoreEvent(kind="turn_routing", text=label))
        elif kind == "turn_end":
            self._emit(CoreEvent(
                kind="turn_end",
                stop_reason=event.stop_reason or "",
            ))
            if event.stop_reason in ("end_turn", "interrupted"):
                self._is_idle = True
                # Drafts that arrived during the turn are held client-side
                # (see #queued-bar in web_chat.html) and sent only when the
                # user explicitly clicks the queued pill's stop button —
                # the backend never auto-dispatches them on turn_end. Any
                # entries in self._pending_user_messages are leftovers from
                # racy POSTs and are discarded silently.
                self._pending_user_messages.clear()
                # Wake stop_model() (and any other waiter) *after* we've
                # cleared the queue and flipped _is_idle, so a follow-up
                # /send that unblocks here sees a fully-idle controller.
                self._idle_event.set()
                self._emit(CoreEvent(kind="ready"))
        elif kind == "session_terminated":
            self._emit(CoreEvent(kind="session_terminated"))
        elif kind == "history_recovered":
            # A broken history was auto-flattened so the turn could proceed.
            # Surfaced as a notice with the dedicated "recovery" severity so
            # the frontend can phrase it per the user's verbosity setting.
            self._emit(CoreEvent(
                kind="notice", severity="recovery", text=event.text or "",
            ))
        elif kind == "error":
            self._emit(CoreEvent(kind="error", text=event.error or ""))

    def set_messaging_target(self, messenger, recipient: str | None) -> None:
        """Update the outbound-messaging destination for the live session, so a
        contact connected in settings takes effect without rebuilding the
        controller. Passing a falsy recipient (or None messenger) disables
        sending until one is set again."""
        self._messenger = messenger
        self._message_recipient = (recipient or "").strip() or None

    def _deliver_message(self, text: str, subject: str | None = None) -> tuple[bool, str]:
        """Send an outbound text to the user via the wired messenger. Returns
        (ok, human_note). Never raises — a missing messenger/recipient or a
        transport failure comes back as (False, friendly reason) so callers can
        report it to the model or surface it in the UI without crashing a run."""
        body = (text or "").strip()
        if not body:
            return False, "no message text to send"
        if self._messenger is None:
            return False, "messaging isn't set up on this server"
        if not self._message_recipient:
            return False, "no messaging contact is connected for this user"
        try:
            self._messenger.send(self._message_recipient, body, subject=subject)
        except Exception as exc:
            return False, str(exc)
        return True, "message sent to the user"

    def _handle_tool_use(self, event: BackendEvent) -> None:
        tool_name = event.tool_name or "tool"
        tool_input = event.tool_input or {}
        details = format_tool_details(tool_name, tool_input)
        self._emit(CoreEvent(
            kind="tool_call",
            tool_name=tool_name,
            tool_details=details,
            tool_detail_full=_full_detail_text(tool_input),
        ))
        if not event.expects_response:
            # Server-side / provider-managed tool: display only.
            return
        # CompleteOnboarding is a client tool the model calls once, after its
        # closing message, to mark first-time onboarding finished. Persist the
        # flag and withdraw the tool so it can't be called again. Handled here
        # (not via the gateway) since it's controller/session state, not a
        # backend data mutation.
        if tool_name == "CompleteOnboarding":
            self._onboarding.mark_complete()
            self._set_terminal_tool(False)
            # Signal-only notice: tell the frontend onboarding is over so it can
            # drop onboarding-only affordances (mirrors the "onboarding" signal).
            self._emit(CoreEvent(kind="notice", severity="onboarding_done"))
            self._emit(CoreEvent(
                kind="tool_result",
                tool_name=tool_name,
                tool_result_summary="onboarding complete",
            ))
            try:
                self._backend.submit(BackendEvent(
                    kind="tool_send_response",
                    tool_use_id=event.tool_use_id,
                    tool_result="Onboarding marked complete. Do not call this tool again.",
                ))
            except Exception as exc:
                self._emit(CoreEvent(kind="error", text=f"tool result send failed: {exc}"))
            return
        # SubmitResult is the terminal tool of a headless background-agent run:
        # the worker calls it once to deliver its result and finish. We surface
        # the structured payload as an `agent_result` CoreEvent for the runner's
        # collector and deliberately do NOT submit a tool response — there is
        # nothing for the worker to do next, so the runner tears the session
        # down rather than paying for a dangling continuation turn.
        if tool_name == "SubmitResult":
            payload = tool_input if isinstance(tool_input, dict) else {}
            summary = (payload.get("summary") or "").strip()
            # If the worker chose to notify the user, deliver it now. The send
            # result is recorded on the event (and so the run transcript) but
            # never blocks teardown — a failed notification doesn't fail the run.
            message = (payload.get("message_to_user") or "").strip()
            message_status = None
            if message:
                ok, note = self._deliver_message(message)
                message_status = "sent" if ok else f"not sent: {note}"
            self._emit(CoreEvent(
                kind="tool_result",
                tool_name=tool_name,
                tool_result_summary=(
                    f"submitted result (message {message_status})"
                    if message_status else "submitted result"
                ),
                tool_detail_full=_full_detail_text(payload),
            ))
            self._emit(CoreEvent(
                kind="agent_result",
                text=summary,
                payload=payload,
            ))
            return
        # WebSearch is a client tool backed by the Haiku sub-agent rather than
        # the HTTP gateway: hand it the request, get back a compact digest +
        # Sources string, and pass that to the model. The bulky raw results
        # never touch this (re-cached) conversation.
        if tool_name == "WebSearch" and self._web_search_agent is not None:
            digest = self._web_search_agent.search(
                tool_input.get("request") or "",
                session_id=getattr(self._backend, "session_id", None),
            )
            self._emit(CoreEvent(
                kind="tool_result",
                tool_name=tool_name,
                tool_result_summary="searched the web",
                tool_detail_full=_full_detail_text(digest),
            ))
            try:
                self._backend.submit(BackendEvent(
                    kind="tool_send_response",
                    tool_use_id=event.tool_use_id,
                    tool_result=digest,
                ))
            except Exception as exc:
                self._emit(CoreEvent(kind="error", text=f"tool result send failed: {exc}"))
            return
        # SendMessage is a client tool (like WebSearch) handled here rather than
        # forwarded to the data backend: it pushes a short text to the user's
        # phone / messaging app via the wired messenger. Available to both
        # interactive Aime and background agents; when no messenger/recipient is
        # wired in it returns a friendly result so the model can fall back to
        # telling the user in chat instead.
        if tool_name == "SendMessage":
            ok, note = self._deliver_message(
                tool_input.get("text") or "", tool_input.get("subject"),
            )
            self._emit(CoreEvent(
                kind="tool_result",
                tool_name=tool_name,
                tool_result_summary=note,
                tool_detail_full=_full_detail_text(tool_input),
            ))
            result_text = (
                "Message delivered to the user."
                if ok else
                f"Message NOT delivered ({note}). Tell the user in this chat instead."
            )
            try:
                self._backend.submit(BackendEvent(
                    kind="tool_send_response",
                    tool_use_id=event.tool_use_id,
                    tool_result=result_text,
                ))
            except Exception as exc:
                self._emit(CoreEvent(kind="error", text=f"tool result send failed: {exc}"))
            return
        # Commitment-pattern tools are computed in Python over get_events (see
        # CommitmentService); they return a ready-to-read text digest, never raw
        # JSON, so they bypass the gateway and format_tool_result_for_model.
        if tool_name in ("GetCommitmentHistory", "GetPatternSummary", "GetRecentActivity"):
            digest = self._run_commitment_tool(tool_name, tool_input)
            self._emit(CoreEvent(
                kind="tool_result",
                tool_name=tool_name,
                tool_result_summary=format_tool_response(tool_name, digest),
                tool_detail_full=_full_detail_text(digest),
            ))
            try:
                self._backend.submit(BackendEvent(
                    kind="tool_send_response",
                    tool_use_id=event.tool_use_id,
                    tool_result=digest,
                ))
            except Exception as exc:
                self._emit(CoreEvent(kind="error", text=f"tool result send failed: {exc}"))
            return
        # Event reminders are client tools too: handled in-process against the
        # user's ScheduleStore via the injected ReminderService, never forwarded
        # to the data backend. Same shape as the WebSearch/SendMessage branches.
        if tool_name in ("CreateReminder", "ListReminders", "DeleteReminder"):
            ui_summary, model_text = self._run_reminder_tool(tool_name, tool_input)
            self._emit(CoreEvent(
                kind="tool_result",
                tool_name=tool_name,
                tool_result_summary=ui_summary,
                tool_detail_full=_full_detail_text(model_text),
            ))
            try:
                self._backend.submit(BackendEvent(
                    kind="tool_send_response",
                    tool_use_id=event.tool_use_id,
                    tool_result=model_text,
                ))
            except Exception as exc:
                self._emit(CoreEvent(kind="error", text=f"tool result send failed: {exc}"))
            return
        result = self._tools.execute(tool_name, tool_input)
        summary = format_tool_response(tool_name, result)
        self._emit(CoreEvent(
            kind="tool_result",
            tool_name=tool_name,
            tool_result_summary=summary,
            tool_detail_full=_full_detail_text(result),
        ))
        # For the high-volume read tools, hand the model a compact text view
        # instead of raw JSON — same information, far fewer cached tokens on
        # every subsequent turn. Other tools (and the UI summary above) keep
        # the raw result. A string result is forwarded verbatim by the backend.
        model_result = format_tool_result_for_model(tool_name, result)
        # Auto-attach commitment history for any returned events that belong to
        # a commitment, so the model already has the pattern context instead of
        # firing a follow-up GetCommitmentHistory per id (slow back-and-forth).
        if tool_name == "FilterUsersEvents" and isinstance(model_result, str):
            model_result = self._attach_commitment_histories(result, model_result)
        try:
            self._backend.submit(BackendEvent(
                kind="tool_send_response",
                tool_use_id=event.tool_use_id,
                tool_result=result if model_result is None else model_result,
            ))
        except Exception as exc:
            self._emit(CoreEvent(kind="error", text=f"tool result send failed: {exc}"))

    def _attach_commitment_histories(self, result, model_result: str) -> str:
        """Append a commitment-history digest for each commitment_id present in a
        FilterUsersEvents result. Best-effort: any failure (or no commitments in
        the result) returns the unmodified text so the events read still lands.

        The digests are dense one-line-per-instance text and share the single
        fetch CommitmentService already does, so this adds the pattern context
        the model usually needs next without a per-id round-trip or much context."""
        try:
            events = _events_from(result)
            ids = [
                (e.get("commitment_id") or "").strip()
                for e in events if isinstance(e, dict)
            ]
            histories = self._commitments.histories_for(
                ids, limit=_AUTO_HISTORY_LIMIT
            )
        except Exception:
            return model_result
        if not histories:
            return model_result
        sections = [model_result, "", "Commitment history (auto-attached):"]
        for digest in histories.values():
            sections.append("")
            sections.append(digest)
        return "\n".join(sections)

    def _run_commitment_tool(self, tool_name: str, tool_input: dict) -> str:
        """Dispatch a commitment-pattern tool to CommitmentService and return its
        text digest. Any failure becomes a clean `Error: ...` string so the model
        can explain it rather than the turn breaking."""
        inp = tool_input or {}
        try:
            if tool_name == "GetCommitmentHistory":
                return self._commitments.commitment_history(
                    inp.get("commitment_id", ""),
                    since_date=inp.get("since_date", ""),
                    limit=int(inp.get("limit", 0) or 0),
                )
            if tool_name == "GetPatternSummary":
                return self._commitments.pattern_summary(
                    commitment_id=inp.get("commitment_id", ""),
                    category=inp.get("category", ""),
                    since_date=inp.get("since_date", ""),
                )
            # GetRecentActivity
            return self._commitments.recent_activity(
                category=inp.get("category", ""),
                since_date=inp.get("since_date", ""),
                limit=int(inp.get("limit", 20) or 0),
            )
        except Exception as exc:
            return f"Error: {exc}"

    def _run_reminder_tool(self, tool_name: str, tool_input: dict) -> tuple[str, str]:
        """Run a reminder client tool against the user's ScheduleStore. Returns
        ``(ui_summary, model_text)`` — a one-line summary for the activity feed
        and the text result handed back to the model. Every failure becomes a
        clean string the model can relay, never an exception that breaks the
        turn."""
        if self._reminder_service is None:
            return ("reminders unavailable",
                    "Reminders aren't available in this session.")
        inp = tool_input or {}
        try:
            if tool_name == "CreateReminder":
                res = self._reminder_service.create(
                    event_id=inp.get("event_id"),
                    days_before=inp.get("days_before", 0),
                    at_time=(inp.get("at_time") or None),
                    tz=self._client_tz,
                )
                if not res.get("ok"):
                    return ("reminder not set", f"Error: {res.get('error')}")
                title = res.get("event_title") or "the event"
                return (f"reminder set · {res.get('lead')}",
                        f"Reminder set for \"{title}\" — {res.get('lead')}. "
                        f"(reminder_id: {res.get('reminder_id')})")

            if tool_name == "ListReminders":
                event_id = inp.get("event_id")
                items = self._reminder_service.list(event_id=event_id)
                if not items:
                    return ("0 reminders", "No reminders set.")
                lines = [f"{len(items)} reminder{'s' if len(items) != 1 else ''}:"]
                for it in items:
                    title = it.get("event_title") or f"event #{it.get('event_id')}"
                    off = "" if it.get("enabled", True) else " [disabled]"
                    lines.append(
                        f"• {it.get('reminder_id')} — \"{title}\" "
                        f"(event #{it.get('event_id')}): {it.get('lead')}{off}")
                return (f"{len(items)} reminder{'s' if len(items) != 1 else ''}",
                        "\n".join(lines))

            # DeleteReminder
            res = self._reminder_service.delete(inp.get("reminder_id", ""))
            if not res.get("ok"):
                return ("reminder not removed", f"Error: {res.get('error')}")
            return ("reminder removed", "Reminder deleted.")
        except Exception as exc:
            return ("reminder error", f"Error: {exc}")
