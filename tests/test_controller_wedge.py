"""The controller must never end up permanently "busy" after a failure.

A turn is claimed busy *before* dispatch and released only on `turn_end`. So
anything that kills the stream worker, or that throws between the claim and the
backend submit, used to leave `_is_idle` False for the life of the process:
every later message queued behind a turn that could never end, and the composer
sat on "Sending…" until the container was restarted. These cover the guards that
make those paths recoverable.
"""

from provider_backend import BackendEvent
from aime.controller import ConversationController


class _FakeBackend:
    conversations_dir = None

    def __init__(self, submit_error=None):
        self.submitted = []
        self._submit_error = submit_error

    def submit(self, event):
        if self._submit_error is not None:
            raise self._submit_error
        self.submitted.append(event)

    def reset(self):
        pass


def _controller(backend=None):
    backend = backend or _FakeBackend()
    events = []
    c = ConversationController(
        backend=backend,
        tool_gateway=object(),      # untouched: first-interaction bootstrap is off
        worker_spawner=lambda fn: None,
    )
    c._user_first_interaction = False
    c.subscribe(events.append)
    return c, backend, events


# --- a raising subscriber must not take the session down -------------------

def test_raising_subscriber_does_not_break_dispatch():
    c, backend, events = _controller()
    c.subscribe(lambda e: (_ for _ in ()).throw(RuntimeError("bad frontend")))
    healthy = []
    c.subscribe(healthy.append)

    c.dispatch_input("hello")

    # The message still reached the model, and the *other* subscribers still saw
    # the event — one broken consumer can't starve the rest.
    assert [e.text for e in backend.submitted] == ["hello"]
    assert "user_message_shown" in [e.kind for e in healthy]


def test_raising_subscriber_does_not_kill_the_stream_worker():
    c, backend, events = _controller()
    boom = []

    def explode(e):
        if e.kind == "assistant_text":
            boom.append(e)
            raise RuntimeError("render blew up")

    c.subscribe(explode)
    c.dispatch_input("hello")
    assert c.is_idle is False               # turn claimed

    # A turn whose middle event blows up must still reach turn_end and go idle.
    c._handle_backend_event(BackendEvent(kind="assistant_send_text", text="hi"))
    c._handle_backend_event(BackendEvent(kind="turn_end", stop_reason="end_turn"))

    assert boom, "the failing subscriber should have been exercised"
    assert c.is_idle is True                # composer unblocks


def test_event_handler_failure_does_not_retire_the_worker():
    """run_stream_loop keeps consuming after one event blows up — otherwise no
    turn_end can ever be delivered and the turn stays claimed forever."""
    c, backend, events = _controller()

    class _Stream:
        def stream(self):
            yield BackendEvent(kind="assistant_send_text", text="hi")
            yield BackendEvent(kind="turn_end", stop_reason="end_turn")

        def submit(self, event):
            pass

        def reset(self):
            pass

    c._backend = _Stream()
    c.dispatch_input("hello")
    assert c.is_idle is False

    c.subscribe(lambda e: (_ for _ in ()).throw(RuntimeError("nope"))
                if e.kind == "assistant_text" else None)
    c.run_stream_loop()

    assert c.is_idle is True


# --- a failed send must release the claim ----------------------------------

def test_submit_failure_releases_the_turn():
    c, backend, events = _controller(_FakeBackend(submit_error=RuntimeError("api down")))
    c.dispatch_input("hello")
    assert c.is_idle is True
    assert "error" in [e.kind for e in events]


def test_bootstrap_failure_still_sends_the_message(monkeypatch):
    """The first message of a chat reads opening context through the tool
    gateway. That is documented best-effort: a failure there costs the model
    some context, never the user's message — and never the turn state, which
    is already claimed busy by the time it runs."""
    import aime.controller as controller_mod

    def boom(_gateway):
        raise RuntimeError("data backend down")

    monkeypatch.setattr(controller_mod, "bootstrap_special_topics", boom)

    c, backend, events = _controller()
    c._user_first_interaction = True

    c.dispatch_input("hello")

    assert [e.text for e in backend.submitted] == ["hello"]   # message got through
    assert c.is_idle is False                                 # turn is live, not wedged
