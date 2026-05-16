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

from dataclasses import dataclass
from typing import Callable, Literal

from provider_backend import AgentBackend, BackendEvent, SessionInfo

from .tool_gateway import ToolGateway
from .tool_formatting import format_tool_details, format_tool_response
from .onboarding import (
    bootstrap_special_topics,
    is_first_conversation,
    ONBOARDING_PROMPT,
)


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
]


Severity = Literal["info", "warning", "error", "success"]
RestartReason = Literal["reset", "load"]


@dataclass
class CoreEvent:
    kind: CoreEventKind
    text: str = ""
    tool_name: str = ""
    tool_details: str = ""
    tool_result_summary: str = ""
    severity: Severity = "info"
    restart_reason: RestartReason | None = None
    stop_reason: str = ""
    # Set on events emitted during /load history replay. Lets the frontend
    # skip live-only affordances like the "thinking…" placeholder.
    from_replay: bool = False


Subscriber = Callable[[CoreEvent], None]
WorkerSpawner = Callable[[Callable[[], None]], None]


class ConversationController:
    def __init__(
        self,
        backend: AgentBackend,
        tool_gateway: ToolGateway,
        worker_spawner: WorkerSpawner,
    ):
        self._backend = backend
        self._tools = tool_gateway
        self._spawn_worker = worker_spawner
        self._subscribers: list[Subscriber] = []
        # Conversation-level state. Presentation flags (e.g. whether the
        # "thinking…" line is visible) live in the frontend, not here.
        self._is_idle = True
        self._pending_user_messages: list[str] = []
        self._user_first_interaction = True
        self._log_model_thinking = False

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
        self._maybe_start_onboarding()

    def _maybe_start_onboarding(self) -> None:
        if not is_first_conversation(self._backend, self._tools):
            return
        bootstrap = bootstrap_special_topics(self._tools)
        if bootstrap:
            self._backend.set_session_context(bootstrap)
        # bootstrap already ran — don't repeat on first user message
        self._user_first_interaction = False
        try:
            self._backend.submit(BackendEvent(
                kind="system_send_message", text=ONBOARDING_PROMPT
            ))
            self._is_idle = False
        except Exception as exc:
            self._emit(CoreEvent(kind="error", text=f"onboarding send failed: {exc}"))

    def shutdown(self) -> None:
        try:
            self._backend.shutdown()
        except Exception:
            pass

    # --- input ---

    def dispatch_input(self, raw: str) -> bool:
        """Process a line of user input (slash commands or plain text).
        Returns True if the frontend should quit the app."""
        text = (raw or "").strip()
        if not text:
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
        self.send_user_message(text)
        return False

    def send_user_message(self, text: str) -> None:
        """Send (or queue) a plain user message without slash parsing."""
        if not self._is_idle:
            self._pending_user_messages.append(text)
            self._emit(CoreEvent(kind="user_message_queued", text=text))
            return
        self._dispatch_user_message(text)

    def _dispatch_user_message(self, text: str) -> None:
        self._emit(CoreEvent(kind="user_message_shown", text=text))
        if self._user_first_interaction:
            bootstrap = bootstrap_special_topics(self._tools)
            if bootstrap:
                self._backend.set_session_context(bootstrap)
            self._user_first_interaction = False
        try:
            self._backend.submit(BackendEvent(kind="user_send_message", text=text))
            self._is_idle = False
        except Exception as exc:
            self._emit(CoreEvent(kind="error", text=f"send failed: {exc}"))

    # --- session operations ---

    def reset(self) -> None:
        self._backend.reset()
        self._reset_internal_state()
        # session_restart = "clear the transcript / wipe presentation state".
        # The banner text follows separately as a notice so the frontend can
        # render it after the clear.
        self._emit(CoreEvent(kind="session_restart", restart_reason="reset"))
        self._emit(CoreEvent(
            kind="notice",
            severity="warning",
            text="The current conversation has ended because you typed "
                 "'/reset'. Begin a new conversation.",
        ))
        self._spawn_worker(self.run_stream_loop)

    def load(self, session_id: str) -> None:
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
        self._emit(CoreEvent(
            kind="notice",
            severity="success",
            text=f"Loaded conversation '{session_id}'. Continue where you left off.",
        ))
        self._spawn_worker(self.run_stream_loop)

    def _reset_internal_state(self) -> None:
        self._user_first_interaction = True
        self._is_idle = True
        self._pending_user_messages = []

    # --- queries used by frontends (e.g. autocomplete) ---

    def list_sessions(self) -> list[SessionInfo]:
        return self._backend.list_sessions()

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
        elif kind == "turn_end":
            self._emit(CoreEvent(
                kind="turn_end",
                stop_reason=event.stop_reason or "",
            ))
            if event.stop_reason == "end_turn":
                self._is_idle = True
                if self._pending_user_messages:
                    next_text = self._pending_user_messages.pop(0)
                    self._dispatch_user_message(next_text)
                else:
                    self._emit(CoreEvent(kind="ready"))
        elif kind == "session_terminated":
            self._emit(CoreEvent(kind="session_terminated"))
        elif kind == "error":
            self._emit(CoreEvent(kind="error", text=event.error or ""))

    def _handle_tool_use(self, event: BackendEvent) -> None:
        tool_name = event.tool_name or "tool"
        tool_input = event.tool_input or {}
        details = format_tool_details(tool_name, tool_input)
        self._emit(CoreEvent(
            kind="tool_call",
            tool_name=tool_name,
            tool_details=details,
        ))
        if not event.expects_response:
            # Server-side / provider-managed tool: display only.
            return
        result = self._tools.execute(tool_name, tool_input)
        summary = format_tool_response(tool_name, result)
        if summary:
            self._emit(CoreEvent(
                kind="tool_result",
                tool_name=tool_name,
                tool_result_summary=summary,
            ))
        try:
            self._backend.submit(BackendEvent(
                kind="tool_send_response",
                tool_use_id=event.tool_use_id,
                tool_result=result,
            ))
        except Exception as exc:
            self._emit(CoreEvent(kind="error", text=f"tool result send failed: {exc}"))
