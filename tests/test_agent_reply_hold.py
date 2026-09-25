"""An agent's message holds the thread open until the user answers it.

A background agent's output is a report the user is expected to reply to
whenever they get to it — but the idle/day rollover treated it like any other
activity, so a reply an hour later silently landed on a *fresh* session with the
agent's message no longer in the model's context (and, across midnight, no
longer on screen). The user answers, and Aime has no idea what about.

So an agent message arms a hold that suppresses both rolls until the reply
arrives. A message Aime sends in its own voice — a scheduler reminder, the
interactive SendMessage tool — deliberately does not: those should still give a
returning user a fresh Today, which is what makes an unanswered reminder feel
like a message left waiting rather than a live conversation.
"""

import datetime
import time

import pytest

import aime.controller as controller_mod
import aime.encryption as _enc
from provider_backend import AnthropicMessagesBackend
from tests.test_controller_continuous import _FakeBackend, _controller, _kinds


def _backend(tmp_path, monkeypatch, *, dek=None):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    b = AnthropicMessagesBackend(
        system_prompt="sys", model="model", schema_files=[],
        conversations_dir=str(tmp_path), dek=dek or _enc.generate_dek(),
    )
    b.new_session()
    return b


@pytest.fixture(autouse=True)
def _pin_rollover(monkeypatch):
    monkeypatch.setattr(controller_mod, "IDLE_ROLLOVER_SECONDS", 3600)
    monkeypatch.setattr(controller_mod, "DAY_ROLL_MIN_GAP_SECONDS", 1800)


def _stale(c, monkeypatch, *, seconds=7200, same_day=True):
    """Put the session well past the idle threshold, optionally pinning the
    calendar day so only the idle gap can trigger a roll."""
    c._last_activity = time.time() - seconds
    if same_day:
        monkeypatch.setattr(c, "_local_date", lambda epoch: datetime.date(2026, 1, 1))


def test_agent_message_holds_the_session_open_for_a_late_reply(monkeypatch):
    c, backend, _ = _controller(messages=[{"role": "user", "content": []}])
    c.record_proactive_message("Your weekly review is ready.", source="agent")
    _stale(c, monkeypatch)

    c.dispatch_input("thanks — what did it say about Thursday?")

    # No roll: the reply lands in the same session, with the agent's message
    # still in history for the model to answer from.
    assert backend.reset_calls == 0
    assert "Your weekly review is ready." in backend.appended


def test_a_normal_aime_message_still_rolls(monkeypatch):
    c, backend, _ = _controller(messages=[{"role": "user", "content": []}])
    c.record_proactive_message("Don't forget the dentist at 3.")
    _stale(c, monkeypatch)

    c.dispatch_input("ok")

    assert backend.reset_calls == 1


def test_the_hold_is_released_by_the_reply(monkeypatch):
    """One late reply is what the hold is for — it must not pin the thread
    forever, or the user never gets a fresh session again."""
    c, backend, _ = _controller(messages=[{"role": "user", "content": []}])
    c.record_proactive_message("Run finished.", source="agent")
    _stale(c, monkeypatch)
    c.dispatch_input("got it")
    assert backend.reset_calls == 0
    assert backend.agent_reply_pending is False

    # Next idle gap behaves normally again. (The no-op worker never emits
    # turn_end, so release the turn claim by hand first.)
    c._is_idle = True
    c._idle_event.set()
    _stale(c, monkeypatch)
    c.dispatch_input("morning")
    assert backend.reset_calls == 1


def test_the_hold_also_suppresses_the_day_roll(monkeypatch):
    """The day roll is the more destructive of the two — it clears the
    transcript — so an overnight agent message must survive it."""
    c, backend, events = _controller(messages=[{"role": "user", "content": []}])
    c.record_proactive_message("Overnight sync done.", source="agent")
    c._last_activity = time.time() - 7200
    # Yesterday → today.
    days = {}
    monkeypatch.setattr(
        c, "_local_date",
        lambda epoch: days.setdefault(epoch, datetime.date(2026, 1, 1))
        if epoch < time.time() - 3600 else datetime.date(2026, 1, 2),
    )
    events.clear()

    c.dispatch_input("what changed?")

    assert backend.reset_calls == 0
    assert "session_restart" not in _kinds(events)


def test_an_unanswered_reminder_across_a_gap_is_not_held(monkeypatch):
    """The contrast case the feature is defined against: Aime's own message
    leaves the thread rollable, so the user comes back to a fresh Today."""
    c, backend, _ = _controller(messages=[{"role": "user", "content": []}])
    c.record_proactive_message("Leaving in 20 minutes?")
    assert backend.agent_reply_pending is False
    _stale(c, monkeypatch)
    c.dispatch_input("yep")
    assert backend.reset_calls == 1


def test_agent_message_arriving_mid_turn_still_holds_when_flushed():
    """A message that lands while a turn is in flight is stashed and flushed on
    turn_end — the source has to survive that round trip or the hold is lost."""
    c, backend, _ = _controller(messages=[{"role": "user", "content": []}])
    c._is_idle = False
    assert c.deliver_inline_proactive("Report ready.", source="agent") is True
    assert c._pending_proactive == [("Report ready.", "agent")]

    c._is_idle = True
    c._flush_pending_proactive()

    assert backend.agent_reply_pending is True
    assert "Report ready." in backend.appended


def test_mid_turn_normal_message_flushes_without_a_hold():
    c, backend, _ = _controller(messages=[{"role": "user", "content": []}])
    c._is_idle = False
    c.deliver_inline_proactive("Dentist at 3.")
    assert c._pending_proactive == [("Dentist at 3.", "assistant")]

    c._is_idle = True
    c._flush_pending_proactive()

    assert backend.agent_reply_pending is False


def test_a_backend_without_the_hold_still_rolls(monkeypatch):
    """The hold is read defensively (not every backend persists one), so a
    backend lacking it must fall back to normal rollover, not to never rolling."""
    class _Bare(_FakeBackend):
        agent_reply_pending = property(lambda self: (_ for _ in ()).throw(AttributeError))

    c, backend, _ = _controller(messages=[{"role": "user", "content": []}])
    monkeypatch.setattr(c, "_awaiting_agent_reply", lambda: False)
    _stale(c, monkeypatch)
    c.dispatch_input("hello")
    assert backend.reset_calls == 1


# --- the agent-reply hold, at the persistence layer -------------------------
#
# The hold has to survive a process restart and an offline delivery: an agent
# finishing while the user is away is exactly the case where the reply comes
# long after, and if the flag lived only in memory that thread would roll on the
# next open — losing the message the user came back to answer.

def test_the_reply_hold_round_trips_through_a_saved_session(tmp_path, monkeypatch):
    dek = _enc.generate_dek()
    b = _backend(tmp_path, monkeypatch, dek=dek)
    b.append_assistant_message("Weekly review is ready.")
    b.set_agent_reply_pending(True)
    session_id = b.session_id

    fresh = _backend(tmp_path, monkeypatch, dek=dek)
    fresh.load_session(session_id)
    assert fresh.agent_reply_pending is True


def test_releasing_the_hold_persists_too(tmp_path, monkeypatch):
    dek = _enc.generate_dek()
    b = _backend(tmp_path, monkeypatch, dek=dek)
    b.append_assistant_message("Run finished.")
    b.set_agent_reply_pending(True)
    b.set_agent_reply_pending(False)
    session_id = b.session_id

    fresh = _backend(tmp_path, monkeypatch, dek=dek)
    fresh.load_session(session_id)
    assert fresh.agent_reply_pending is False


def test_a_session_saved_before_the_flag_existed_does_not_hold(tmp_path, monkeypatch):
    """A file written before the flag existed has no such key at all. It must
    read as "no hold" — a legacy thread pinned forever would never roll again."""
    import json

    dek = _enc.generate_dek()
    b = _backend(tmp_path, monkeypatch, dek=dek)
    b.append_assistant_message("Older message.")
    session_id = b.session_id

    # Rewrite the saved file exactly as a pre-flag build would have left it.
    path = next(tmp_path.glob(f"{session_id}*"))
    aad = session_id.encode("utf-8")
    data = json.loads(_enc.decrypt_blob(dek, path.read_bytes(), aad=aad))
    assert data.pop("agent_reply_pending") is False
    path.write_bytes(_enc.encrypt_blob(dek, json.dumps(data).encode(), aad=aad))

    fresh = _backend(tmp_path, monkeypatch, dek=dek)
    fresh.load_session(session_id)
    assert fresh.agent_reply_pending is False


def test_a_fresh_session_never_inherits_a_hold(tmp_path, monkeypatch):
    b = _backend(tmp_path, monkeypatch)
    b.set_agent_reply_pending(True)
    b.new_session()
    assert b.agent_reply_pending is False


def test_offline_delivery_can_arm_the_hold(tmp_path, monkeypatch):
    """The agent finished while the user was away: the message goes straight to
    disk, and the hold has to go with it."""
    import aime.encryption as _enc
    from provider_backend import append_proactive_message_offline

    dek = _enc.generate_dek()
    assert append_proactive_message_offline(
        str(tmp_path), dek, "Your review is ready.", pin_reply=True)

    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    b = AnthropicMessagesBackend(
        system_prompt="sys", model="model", schema_files=[],
        conversations_dir=str(tmp_path), dek=dek,
    )
    sid = b.list_sessions()[0].id
    b.load_session(sid)
    assert b.agent_reply_pending is True


def test_offline_delivery_of_a_normal_message_arms_nothing(tmp_path, monkeypatch):
    import aime.encryption as _enc
    from provider_backend import append_proactive_message_offline

    dek = _enc.generate_dek()
    assert append_proactive_message_offline(
        str(tmp_path), dek, "Dentist at 3.")

    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    b = AnthropicMessagesBackend(
        system_prompt="sys", model="model", schema_files=[],
        conversations_dir=str(tmp_path), dek=dek,
    )
    sid = b.list_sessions()[0].id
    b.load_session(sid)
    assert b.agent_reply_pending is False
