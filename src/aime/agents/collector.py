"""The headless analogue of a frontend: a ``CoreEvent`` subscriber that watches
a background-agent run and assembles its result.

Where an interactive frontend renders the controller's event stream to a user,
``ResultCollector`` consumes the same stream to decide when the run is finished
and what it produced. It is the only thing the ``BackgroundAgentRunner``
subscribes to the controller.

It tracks three things:
  * the structured result, captured when the worker emits ``agent_result``
    (i.e. calls SubmitResult);
  * a fallback summary — the worker's last block of assistant text — so a
    partial run still has something human to report;
  * a full transcript of the run, serialized for the persisted run record.

Coordination with the runner is via a condition variable: the runner blocks in
``wait_progress`` until the run terminates or the worker finishes a reply round
without submitting (so the runner can nudge it or give up).
"""

import datetime
import threading
import time

from ..controller import CoreEvent


def _event_to_dict(event: CoreEvent) -> dict:
    """Serialize a CoreEvent for the run transcript. Drops attachments (which
    can be large base64 image blobs) and stamps a wall-clock time so the record
    can be replayed/inspected later."""
    return {
        "ts": datetime.datetime.now().isoformat(timespec="milliseconds"),
        "kind": event.kind,
        "text": event.text,
        "tool_name": event.tool_name,
        "tool_details": event.tool_details,
        "tool_result_summary": event.tool_result_summary,
        "tool_detail_full": event.tool_detail_full,
        "severity": event.severity,
        "stop_reason": event.stop_reason,
        "payload": event.payload,
    }


class ResultCollector:
    """Accumulates a background-agent run's output from the CoreEvent stream.

    Thread-safety: ``__call__`` runs on the controller's worker thread while the
    runner blocks in ``wait_progress`` on another; all state is guarded by a
    single condition variable.
    """

    def __init__(self):
        self._cond = threading.Condition()
        self._done = False
        self._completed = False          # True once SubmitResult fired
        self._result_payload: dict | None = None
        self._message_to_user = ""
        self._submitted_summary = ""
        self._last_block_text = ""
        self._idle_rounds = 0            # reply rounds that ended without submit
        self._error: str | None = None
        self._events: list[dict] = []

    # --- subscriber ---

    def __call__(self, event: CoreEvent) -> None:
        with self._cond:
            self._events.append(_event_to_dict(event))
            kind = event.kind
            if kind in ("assistant_text", "assistant_text_end"):
                # Keep the latest non-empty assistant block as the fallback
                # summary. Deltas are recorded in the transcript but ignored
                # here — assistant_text_end carries the authoritative full block.
                text = (event.text or "").strip()
                if text:
                    self._last_block_text = text
            elif kind == "agent_result":
                payload = event.payload or {}
                self._result_payload = (
                    payload.get("result") if isinstance(payload, dict) else None
                )
                # The worker's optional proactive note to the user. Already
                # delivered out of band by the controller; captured here so the
                # caller can also thread it into the user's conversation.
                self._message_to_user = (
                    (payload.get("message_to_user") or "")
                    if isinstance(payload, dict) else ""
                ).strip()
                self._submitted_summary = (
                    event.text or (payload.get("summary") if isinstance(payload, dict) else "") or ""
                ).strip()
                self._completed = True
                self._done = True
                self._cond.notify_all()
            elif kind == "turn_end":
                # A reply round that ended on its own (not mid-tool-loop) without
                # a SubmitResult is an "idle round": the worker stopped talking
                # without delivering. Surface it so the runner can nudge or stop.
                if event.stop_reason == "end_turn" and not self._done:
                    self._idle_rounds += 1
                    self._cond.notify_all()
            elif kind == "error":
                self._error = event.text or "unknown error"
                self._done = True
                self._cond.notify_all()
            elif kind == "session_terminated":
                self._done = True
                self._cond.notify_all()

    # --- runner coordination ---

    def wait_progress(self, seen_rounds: int, timeout: float) -> bool:
        """Block until the run terminates or a new idle round occurs.

        Returns True on progress (the run is done, or ``idle_rounds`` has grown
        past ``seen_rounds``), or False if ``timeout`` elapsed first — which the
        runner treats as a stuck worker.
        """
        deadline = time.monotonic() + timeout
        with self._cond:
            while not self._done and self._idle_rounds <= seen_rounds:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._cond.wait(remaining)
            return True

    # --- read-side (call after the run settles) ---

    @property
    def done(self) -> bool:
        with self._cond:
            return self._done

    @property
    def completed(self) -> bool:
        with self._cond:
            return self._completed

    @property
    def idle_rounds(self) -> int:
        with self._cond:
            return self._idle_rounds

    @property
    def error(self) -> str | None:
        with self._cond:
            return self._error

    @property
    def result_payload(self) -> dict | None:
        with self._cond:
            return self._result_payload

    @property
    def message_to_user(self) -> str:
        """The proactive note the worker sent the user via SubmitResult's
        ``message_to_user`` (empty if none). The caller threads it into the
        user's conversation so it shows inline, like a text Aime sent."""
        with self._cond:
            return self._message_to_user

    @property
    def summary_text(self) -> str:
        """The SubmitResult summary if the worker submitted one, otherwise its
        last block of assistant text as a fallback."""
        with self._cond:
            return self._submitted_summary or self._last_block_text

    def transcript(self) -> list[dict]:
        with self._cond:
            return list(self._events)
