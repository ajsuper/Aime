"""A retired stream worker must exit on its own, not linger until the next message.

Retirement bumps the epoch and pulses the turn trigger — but the session swap
that follows clears that trigger microseconds later, so the outgoing worker
almost never observes its own wake-up. It used to park indefinitely, leaking one
thread per session swap (idle rollover, day roll, /reset, conversation switch);
a production thread dump showed three of them stacked up on a quiet instance.
"""

import threading
import time

import pytest

import aime.encryption as _enc
from provider_backend import AnthropicMessagesBackend, BackendEvent


@pytest.fixture
def backend(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    b = AnthropicMessagesBackend(
        system_prompt="sys", model="model", schema_files=[],
        conversations_dir=str(tmp_path), dek=_enc.generate_dek(),
    )
    b.new_session()
    monkeypatch.setattr(type(b), "STALE_WORKER_POLL_SECONDS", 0.05)
    return b


def _drain(backend, kinds, done):
    for event in backend.stream():
        kinds.append(event.kind)
        if event.kind == "session_terminated":
            break
    done.set()


def test_retired_worker_exits_without_a_new_message(backend):
    kinds, done = [], threading.Event()
    threading.Thread(target=_drain, args=(backend, kinds, done), daemon=True).start()
    time.sleep(0.1)          # let it park on the trigger

    # Exactly the production retirement sequence: pulse, then immediately swap
    # (which clears the trigger the worker was supposed to notice).
    backend._terminate_active_stream()
    backend.new_session()

    assert done.wait(5), "retired worker never noticed it had been replaced"
    assert kinds == ["session_terminated"]


def test_entering_a_temporary_chat_retires_the_worker(backend):
    """Every other swap path retires the outgoing worker by bumping the epoch;
    start_ephemeral_session used not to. The controller's fresh worker joined
    the old one on the *same* epoch, so the old one never recognised itself as
    stale — two live loops sharing one trigger and one message list, both
    waking on the next user message and both running a turn."""
    kinds, done = [], threading.Event()
    threading.Thread(target=_drain, args=(backend, kinds, done), daemon=True).start()
    time.sleep(0.1)          # let it park on the trigger

    backend.start_ephemeral_session()

    assert done.wait(5), "worker survived the switch into a temporary chat"
    assert kinds == ["session_terminated"]


def test_a_proactive_write_never_lands_on_an_unanswered_turn(backend):
    """The wedge that froze a live chat. record_proactive_message checks the
    controller is idle, releases the lock, then appends — so a /send can land in
    between, arming a turn. Appending an assistant message on top of that user
    message left a history the worker refuses to answer."""
    backend.submit(BackendEvent(kind="user_send_message", text="hi"))

    assert backend.append_assistant_message("your 3pm is starting") is False
    assert [m["role"] for m in backend.messages_snapshot()] == ["user"]

    # Once the turn is answered, the proactive echo lands normally — the caller
    # re-stashes it and flushes on the next turn_end, so nothing is lost.
    backend._messages.append({"role": "assistant", "content": [
        {"type": "text", "text": "hello"}]})
    assert backend.append_assistant_message("your 3pm is starting") is True


def test_a_skipped_turn_still_ends_the_turn(backend):
    """A wake-up the worker declines to answer must still produce a turn_end.
    The /send that armed the trigger has already claimed the turn busy on the
    controller; dropping the wake-up silently left that claim outstanding with
    nothing alive to release it — the chat froze on "Sending…" while this worker
    sat in the wait loop looking healthy."""
    kinds, done = [], threading.Event()

    def drain():
        for event in backend.stream():
            kinds.append(event.kind)
            if event.kind in ("turn_end", "session_terminated"):
                break
        done.set()

    threading.Thread(target=drain, daemon=True).start()
    time.sleep(0.1)

    # A history that doesn't end on a user turn, then the wake-up.
    backend._messages.append({"role": "user", "content": [
        {"type": "text", "text": "hi"}]})
    backend._messages.append({"role": "assistant", "content": [
        {"type": "text", "text": "hello"}]})
    backend._turn_trigger.set()

    assert done.wait(5), "the skipped turn never reported back"
    assert kinds == ["turn_end"]


def test_a_reminder_racing_a_send_does_not_freeze_the_chat(backend, monkeypatch):
    """End to end, the production freeze: the scheduler's reminder write lands
    between a /send claiming the turn and the worker picking it up. Before the
    fix the worker skipped the turn in silence and the controller stayed claimed
    busy forever — every later message queued, the composer sat on "Sending…",
    and only a restart cleared it."""
    from aime.controller import ConversationController

    turns = []

    def fake_run_turn():
        turns.append(1)
        yield BackendEvent(kind="assistant_send_text", text="on it")
        yield BackendEvent(kind="turn_end", stop_reason="end_turn")

    monkeypatch.setattr(backend, "_run_turn", fake_run_turn)

    controller = ConversationController(
        backend=backend,
        tool_gateway=object(),
        worker_spawner=lambda fn: threading.Thread(target=fn, daemon=True).start(),
    )
    controller._user_first_interaction = False
    controller.start()
    time.sleep(0.1)

    # The interleaving: /send claims the turn and appends the user message, then
    # the reminder write lands before the worker has picked the turn up.
    controller.send_user_message("are we still on for 3?")
    backend.append_assistant_message("Reminder: your 3pm starts in 5 minutes.")

    deadline = time.time() + 5
    while time.time() < deadline and not controller.is_idle:
        time.sleep(0.05)

    assert controller.is_idle, "controller stayed claimed busy — chat is frozen"
    assert turns, "the user's message was never answered"


def test_live_worker_keeps_waiting(backend):
    kinds, done = [], threading.Event()
    threading.Thread(target=_drain, args=(backend, kinds, done), daemon=True).start()

    # No retirement — the poll must not mistake a quiet session for a dead one.
    time.sleep(0.4)
    assert not done.is_set()
    assert kinds == []
