"""Continuous-thread controller behavior: the idle-gap silent rollover and
inline recording of proactive messages.

The rollover swaps the backend session *invisibly* — a returning user past the
idle threshold starts a fresh, cost-bounded session with no "New conversation"
break (no session_restart / new_session splash). ``record_proactive_message``
threads an out-of-band send into the live session as an assistant turn so it
shows in the chat and the reply has context.
"""

import time

import aime.controller as controller_mod
from provider_backend import BackendEvent
from aime.controller import ConversationController


class _FakeBackend:
    """Records submits and session swaps, with just enough surface for the
    rollover/proactive paths (messages_snapshot, append_assistant_message)."""

    conversations_dir = None

    def __init__(self, messages=None):
        self.submitted = []
        self.reset_calls = 0
        self.appended = []
        self._messages = list(messages or [])

    def submit(self, event):
        self.submitted.append(event)

    def reset(self):
        self.reset_calls += 1
        self._messages = []

    def messages_snapshot(self):
        return list(self._messages)

    def append_assistant_message(self, text):
        self._messages.append({"role": "assistant",
                               "content": [{"type": "text", "text": text}]})
        self.appended.append(text)
        return True


def _controller(messages=None):
    backend = _FakeBackend(messages=messages)
    events = []
    c = ConversationController(
        backend=backend,
        tool_gateway=object(),
        worker_spawner=lambda fn: None,
    )
    c._user_first_interaction = False
    c._maybe_start_onboarding = lambda: None   # avoid tool_gateway reach-in
    c.subscribe(events.append)
    return c, backend, events


def _kinds(events):
    return [e.kind for e in events]


def _user_texts(backend):
    return [e.text for e in backend.submitted if e.kind == "user_send_message"]


# --- idle-gap silent rollover ---------------------------------------------

def test_idle_gap_rolls_to_fresh_session_silently(monkeypatch):
    monkeypatch.setattr(controller_mod, "IDLE_ROLLOVER_SECONDS", 3600)
    c, backend, events = _controller(messages=[{"role": "user", "content": []}])
    # Last activity was two hours ago → the next message should roll over first.
    c._last_activity = time.time() - 7200
    events.clear()

    c.dispatch_input("you still there?")

    assert backend.reset_calls == 1                      # swapped to a fresh session
    assert _user_texts(backend) == ["you still there?"]  # message still delivered
    # Invisible: no transcript clear, no "New conversation" splash.
    assert "session_restart" not in _kinds(events)
    assert not any(
        e.kind == "notice" and e.severity == "new_session" for e in events
    )
    # ...but a subtle session divider marks where the fresh session begins, and
    # it lands above the user's message.
    kinds = _kinds(events)
    assert "session_divider" in kinds
    assert kinds.index("session_divider") < kinds.index("user_message_shown")


def test_no_rollover_within_idle_window(monkeypatch):
    monkeypatch.setattr(controller_mod, "IDLE_ROLLOVER_SECONDS", 3600)
    c, backend, events = _controller(messages=[{"role": "user", "content": []}])
    c._last_activity = time.time() - 60     # only a minute ago
    c.dispatch_input("quick follow-up")
    assert backend.reset_calls == 0


def test_no_rollover_when_session_empty(monkeypatch):
    monkeypatch.setattr(controller_mod, "IDLE_ROLLOVER_SECONDS", 3600)
    c, backend, events = _controller(messages=[])    # nothing to bound
    c._last_activity = time.time() - 7200
    c.dispatch_input("first ever message")
    assert backend.reset_calls == 0


def test_rollover_disabled_when_threshold_zero(monkeypatch):
    monkeypatch.setattr(controller_mod, "IDLE_ROLLOVER_SECONDS", 0)
    c, backend, events = _controller(messages=[{"role": "user", "content": []}])
    c._last_activity = time.time() - 999999
    c.dispatch_input("hello")
    assert backend.reset_calls == 0


def test_explicit_reset_still_announces(monkeypatch):
    # The user-visible reset path must keep its transcript clear + splash; only the
    # silent rollover suppresses them.
    c, backend, events = _controller()
    c.dispatch_input("hi")
    c._handle_backend_event(BackendEvent(kind="turn_end", stop_reason="end_turn"))
    events.clear()
    c.reset()
    assert "session_restart" in _kinds(events)
    assert any(
        e.kind == "notice" and e.severity == "new_session" for e in events
    )


def test_seed_last_activity_from_iso(monkeypatch):
    c, backend, events = _controller()
    c.seed_last_activity("2020-01-01T00:00:00")
    assert c._last_activity < time.time() - 1000   # well in the past


# --- inline proactive recording -------------------------------------------

def test_record_proactive_when_idle_appends_and_emits():
    c, backend, events = _controller()
    events.clear()
    assert c.record_proactive_message("Reminder: gig at 5:30!") is True
    assert backend.appended == ["Reminder: gig at 5:30!"]
    # Surfaced live to the frontend as an Aime bubble.
    assistant = [e for e in events if e.kind == "assistant_text"]
    assert assistant and assistant[-1].text == "Reminder: gig at 5:30!"


def test_record_proactive_skipped_mid_turn():
    c, backend, events = _controller()
    c.dispatch_input("working on it")     # claims the turn (now busy)
    assert c.is_idle is False
    assert c.record_proactive_message("ping") is False
    assert backend.appended == []         # never appended mid-turn


def test_record_proactive_empty_is_noop():
    c, backend, events = _controller()
    assert c.record_proactive_message("  ") is False
    assert backend.appended == []


# --- interactive SendMessage flushes inline on turn end -------------------

def test_sendmessage_flushes_inline_on_turn_end():
    c, backend, events = _controller()
    # A wired messenger so _deliver_message reports success.
    sent = []
    c._messenger = type("M", (), {"send": lambda self, to, text, subject=None: sent.append(text)})()
    c._message_recipient = "tg:123"

    c.dispatch_input("text me a reminder")     # claims the turn
    # Model calls SendMessage mid-turn.
    c._handle_backend_event(BackendEvent(
        kind="assistant_use_tool", tool_name="SendMessage",
        tool_input={"text": "Reminder: gig at 5:30!"},
        tool_use_id="tu_1", expects_response=True,
    ))
    # Delivered out of band, but not yet appended to history (mid-turn).
    assert sent == ["Reminder: gig at 5:30!"]
    assert backend.appended == []

    # Turn ends → the sent text flushes as an inline assistant bubble.
    c._handle_backend_event(BackendEvent(kind="turn_end", stop_reason="end_turn"))
    assert backend.appended == ["Reminder: gig at 5:30!"]
    assert any(
        e.kind == "assistant_text" and e.text == "Reminder: gig at 5:30!"
        for e in events
    )


def test_deliver_inline_proactive_when_idle_records_now():
    c, backend, events = _controller()
    assert c.deliver_inline_proactive("Heads up: 6pm") is True
    assert backend.appended == ["Heads up: 6pm"]


def test_deliver_inline_proactive_when_busy_defers_to_turn_end():
    c, backend, events = _controller()
    c.dispatch_input("hold on")          # claims the turn (busy)
    assert c.deliver_inline_proactive("Reminder fired") is True
    assert backend.appended == []        # not recorded mid-turn
    # Flushes once the turn ends.
    c._handle_backend_event(BackendEvent(kind="turn_end", stop_reason="end_turn"))
    assert backend.appended == ["Reminder fired"]


def test_sendmessage_not_flushed_when_delivery_fails():
    c, backend, events = _controller()
    # No messenger wired → delivery fails → nothing to echo inline.
    c.dispatch_input("text me")
    c._handle_backend_event(BackendEvent(
        kind="assistant_use_tool", tool_name="SendMessage",
        tool_input={"text": "hello"}, tool_use_id="tu_1", expects_response=True,
    ))
    c._handle_backend_event(BackendEvent(kind="turn_end", stop_reason="end_turn"))
    assert backend.appended == []
