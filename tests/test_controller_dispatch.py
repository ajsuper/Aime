"""Concurrency/queue behavior of ConversationController input dispatch.

Covers the hardening that makes input dispatch race-free: a message that arrives
while the model is busy is *queued and then drained* on turn_end (not discarded),
and the claim-or-queue decision is atomic. See controller.send_user_message /
the turn_end handler.
"""

from provider_backend import BackendEvent
from aime.controller import ConversationController


class _FakeBackend:
    """Minimal stand-in: records what the controller submits to the model.
    No real streaming — tests drive turn boundaries by calling the controller's
    backend-event handler directly."""

    conversations_dir = None

    def __init__(self):
        self.submitted = []

    def submit(self, event):
        self.submitted.append(event)

    def reset(self):
        pass


def _controller():
    backend = _FakeBackend()
    events = []
    c = ConversationController(
        backend=backend,
        tool_gateway=object(),       # untouched: first-interaction bootstrap is off
        worker_spawner=lambda fn: None,
    )
    # Skip the first-interaction bootstrap (it would reach into tool_gateway).
    c._user_first_interaction = False
    c.subscribe(events.append)
    return c, backend, events


def _kinds(events):
    return [e.kind for e in events]


def _user_texts(backend):
    return [e.text for e in backend.submitted if e.kind == "user_send_message"]


def test_idle_message_dispatches_immediately():
    c, backend, events = _controller()
    c.dispatch_input("hello")
    assert _user_texts(backend) == ["hello"]
    assert c.is_idle is False
    assert "user_message_shown" in _kinds(events)


def test_message_while_busy_is_queued_not_dropped():
    c, backend, events = _controller()
    c.dispatch_input("first")          # claims the turn
    events.clear()
    c.dispatch_input("second")         # arrives mid-turn
    # Not submitted to the model yet, surfaced as queued, and retained.
    assert _user_texts(backend) == ["first"]
    assert _kinds(events) == ["user_message_queued"]
    assert len(c._pending_user_messages) == 1


def test_queued_message_drains_on_turn_end():
    c, backend, events = _controller()
    c.dispatch_input("first")
    c.dispatch_input("second")         # queued
    events.clear()

    # The turn ends naturally — the queued message must now be dispatched,
    # not discarded, and the controller stays busy for that fresh turn.
    c._handle_backend_event(BackendEvent(kind="turn_end", stop_reason="end_turn"))
    assert _user_texts(backend) == ["first", "second"]
    assert c.is_idle is False
    assert "user_message_shown" in _kinds(events)
    assert "ready" not in _kinds(events)
    assert c._pending_user_messages == []

    # That turn ends with nothing queued → genuinely idle.
    events.clear()
    c._handle_backend_event(BackendEvent(kind="turn_end", stop_reason="end_turn"))
    assert c.is_idle is True
    assert "ready" in _kinds(events)


def test_hidden_prefix_survives_queueing():
    c, backend, events = _controller()
    c.dispatch_input("first")
    c.dispatch_input("second", hidden_prefix="<stale>t9</stale>")
    c._handle_backend_event(BackendEvent(kind="turn_end", stop_reason="end_turn"))
    # The drained message carries its out-of-band prefix to the model (the
    # bubble text stays clean — that's asserted by user_message_shown using the
    # raw text elsewhere; here we check the model-facing submit).
    second = [e for e in backend.submitted if e.kind == "user_send_message"][-1]
    assert "<stale>t9</stale>" in (second.text or "")
    assert second.text.endswith("second")


def test_reset_drops_queued_message():
    c, backend, events = _controller()
    # Make the backend's interrupt resolve the turn synchronously, the way the
    # real stream worker would, so reset()'s stop_model() doesn't block.
    backend.interrupt_turn = lambda: (
        setattr(c, "_is_idle", True), c._idle_event.set())
    c._maybe_start_onboarding = lambda: None   # avoid tool_gateway reach-in

    c.dispatch_input("first")
    c.dispatch_input("leftover")               # queued for the OLD conversation
    assert len(c._pending_user_messages) == 1

    c.reset()
    # The queued message belonged to the conversation we left — it must not be
    # carried into (or dispatched in) the new session.
    assert c._pending_user_messages == []
    assert "leftover" not in _user_texts(backend)
    assert c.is_idle is True
