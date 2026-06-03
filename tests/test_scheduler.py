"""Unit tests for aime.scheduling.scheduler — the tick loop + firing logic.

Driven by fakes (fake auth, recording run_agent/send_message, canned events) and
an explicit ``now``, so no thread, clock, or real backend is involved. Covers
the firing rules the design hinges on: next-slot recurring fires, the grace
window (fire vs missed-advance), once self-delete, and the relative reminder's
window / dedup / re-arm-on-move / orphan-cleanup behavior.
"""

import datetime
import zoneinfo

import pytest

from aime import encryption as enc
from aime.auth import BackgroundUnavailable
from aime.scheduling import store as s
from aime.scheduling.scheduler import Scheduler

TZ = "America/New_York"
ZONE = zoneinfo.ZoneInfo(TZ)
UTC = datetime.timezone.utc


class _User:
    def __init__(self, uid):
        self.id = uid


class FakeAuth:
    """Minimal auth stand-in: a fixed user list and per-user DEKs. A user_id in
    ``unavailable`` raises BackgroundUnavailable from get_dek (pre-v2 account)."""

    def __init__(self, user_ids, unavailable=()):
        self._ids = list(user_ids)
        self._deks = {uid: enc.generate_dek() for uid in user_ids}
        self._unavailable = set(unavailable)

    def list_users(self):
        return [_User(uid) for uid in self._ids]

    def get_dek(self, uid):
        if uid in self._unavailable:
            raise BackgroundUnavailable(uid)
        return self._deks[uid]


@pytest.fixture
def harness(tmp_path):
    """A Scheduler wired to one user (id 1) over tmp storage, with recorders for
    the two actions and a settable event list. Returns (scheduler, ctx)."""
    auth = FakeAuth([1])
    ctx = {
        "runs": [],            # (agent_id, user_id, tz)
        "messages": [],        # (user_id, text)
        "events": [],          # what upcoming_events returns
        "events_fail": False,  # make upcoming_events raise
        "dir": lambda uid: str(tmp_path / str(uid)),
        "auth": auth,
    }

    def upcoming(uid):
        if ctx["events_fail"]:
            raise RuntimeError("backend down")
        return ctx["events"]

    sched = Scheduler(
        auth=auth,
        schedules_dir=ctx["dir"],
        run_agent=lambda *a: ctx["runs"].append(a),
        send_message=lambda *a: ctx["messages"].append(a),
        upcoming_events=upcoming,
        grace=datetime.timedelta(hours=2),
    )
    ctx["store"] = s.ScheduleStore(ctx["dir"](1), auth._deks[1])
    return sched, ctx


def at(y, mo, d, h=0, mi=0, s=0):
    """An aware UTC 'now' built from EST wall-clock — keeps the test cases easy
    to read against America/New_York schedules."""
    return datetime.datetime(y, mo, d, h, mi, s, tzinfo=ZONE).astimezone(UTC)


def recurring_agent():
    return s.make_schedule(
        trigger={"kind": "recurring", "freq": "daily", "time": "07:00"},
        action={"kind": "run_agent", "agent_id": "agent-brief-1"},
        tz=TZ,
    )


def relative_reminder(event_id=42):
    return s.make_schedule(
        trigger={"kind": "relative", "event_id": event_id, "days_before": 3,
                 "at_time": "08:00", "minutes_before": 0},
        action={"kind": "send_message", "message": "Pack for {event_title}!"},
        tz=TZ,
    )


# ── recurring ───────────────────────────────────────────────────────────────

def test_recurring_fires_in_window_and_advances(harness):
    sched, ctx = harness
    rec = recurring_agent()
    rec["state"]["last_run_at"] = at(2026, 6, 2, 7, 0).isoformat()  # fired yesterday
    ctx["store"].save(rec)

    sched.tick(at(2026, 6, 3, 7, 1))  # just after today's 07:00 slot

    assert ctx["runs"] == [("agent-brief-1", 1, TZ)]
    reloaded = ctx["store"].load(rec["schedule_id"])
    # last_run_at advanced to the scheduled instant (07:00), not now (07:01).
    assert datetime.datetime.fromisoformat(
        reloaded["state"]["last_run_at"]) == at(2026, 6, 3, 7, 0)


def test_fresh_schedule_fires_first_occurrence(harness, monkeypatch):
    # Regression: a brand-new schedule (last_run_at is None) created shortly
    # before its first slot must still fire when that slot arrives. Anchoring to
    # `now` instead of created/updated_at made `due` roll to tomorrow the moment
    # the slot passed, so the first fire was silently skipped forever.
    sched, ctx = harness
    # Pin the store's clock so the record's created/updated stamps sit at 06:50,
    # ten minutes before the daily 07:00 slot — exactly the reported scenario.
    monkeypatch.setattr(s, "_utc_now_iso", lambda: at(2026, 6, 3, 6, 50).isoformat())
    rec = recurring_agent()
    ctx["store"].save(rec)
    assert rec["state"]["last_run_at"] is None

    sched.tick(at(2026, 6, 3, 6, 55))     # before the slot: nothing yet
    assert ctx["runs"] == []

    sched.tick(at(2026, 6, 3, 7, 0, 20))  # 20s after the 07:00 slot
    assert ctx["runs"] == [("agent-brief-1", 1, TZ)]


def test_recurring_not_yet_does_not_fire(harness):
    sched, ctx = harness
    rec = recurring_agent()
    rec["state"]["last_run_at"] = at(2026, 6, 3, 7, 0).isoformat()
    ctx["store"].save(rec)

    sched.tick(at(2026, 6, 3, 6, 0))  # before tomorrow's slot
    assert ctx["runs"] == []


def test_recurring_missed_beyond_grace_advances_without_firing(harness):
    sched, ctx = harness
    rec = recurring_agent()
    rec["state"]["last_run_at"] = at(2026, 6, 2, 7, 0).isoformat()
    ctx["store"].save(rec)

    # Machine was off; we wake 5h after the 07:00 slot (grace is 2h).
    sched.tick(at(2026, 6, 3, 12, 0))

    assert ctx["runs"] == []  # no catch-up fire
    reloaded = ctx["store"].load(rec["schedule_id"])
    assert datetime.datetime.fromisoformat(
        reloaded["state"]["last_run_at"]) == at(2026, 6, 3, 7, 0)


def test_disabled_never_fires(harness):
    sched, ctx = harness
    rec = recurring_agent()
    rec["enabled"] = False
    rec["state"]["last_run_at"] = at(2026, 6, 2, 7, 0).isoformat()
    ctx["store"].save(rec)

    sched.tick(at(2026, 6, 3, 7, 1))
    assert ctx["runs"] == []


# ── once ────────────────────────────────────────────────────────────────────

def test_once_fires_then_self_deletes(harness):
    sched, ctx = harness
    rec = s.make_schedule(
        trigger={"kind": "once", "at": "2026-06-10T15:00:00"},
        action={"kind": "send_message", "message": "ping"},
        tz=TZ,
    )
    ctx["store"].save(rec)

    sched.tick(at(2026, 6, 10, 15, 1))
    assert ctx["messages"] == [(1, "ping")]
    assert ctx["store"].load(rec["schedule_id"]) is None  # gone after firing


def test_once_in_future_does_not_fire(harness):
    sched, ctx = harness
    rec = s.make_schedule(
        trigger={"kind": "once", "at": "2026-06-10T15:00:00"},
        action={"kind": "send_message", "message": "ping"},
        tz=TZ,
    )
    ctx["store"].save(rec)

    sched.tick(at(2026, 6, 9, 12, 0))
    assert ctx["messages"] == []
    assert ctx["store"].load(rec["schedule_id"]) is not None


# ── relative (event reminders) ──────────────────────────────────────────────

def _event(event_id=42, date="10/07/2026", time="14:00", title="Trip"):
    return {"id": event_id, "date": date, "time": time, "title": title}


def test_relative_fires_in_window_renders_and_marks(harness):
    sched, ctx = harness
    rec = relative_reminder()
    ctx["store"].save(rec)
    ctx["events"] = [_event()]  # trip 2026-07-10 14:00 → reminder 07-07 08:00

    sched.tick(at(2026, 7, 7, 8, 1))

    assert ctx["messages"] == [(1, "Pack for Trip!")]  # token rendered
    reloaded = ctx["store"].load(rec["schedule_id"])
    start_iso = datetime.datetime(2026, 7, 10, 14, 0, tzinfo=ZONE).isoformat()
    assert reloaded["state"]["sent_for_start"] == start_iso


def test_relative_dedup_does_not_resend(harness):
    sched, ctx = harness
    rec = relative_reminder()
    ctx["store"].save(rec)
    ctx["events"] = [_event()]

    sched.tick(at(2026, 7, 7, 8, 1))
    sched.tick(at(2026, 7, 7, 9, 0))  # later same window, same event start
    assert len(ctx["messages"]) == 1


def test_relative_rearms_when_event_moves(harness):
    sched, ctx = harness
    rec = relative_reminder()
    ctx["store"].save(rec)
    ctx["events"] = [_event()]
    sched.tick(at(2026, 7, 7, 8, 1))           # fires for the 07-10 start
    assert len(ctx["messages"]) == 1

    # Trip moves to 07-20; the 8am/3-days rule re-derives to 07-17 08:00.
    ctx["events"] = [_event(date="20/07/2026", time="10:00")]
    sched.tick(at(2026, 7, 17, 8, 1))
    assert len(ctx["messages"]) == 2


def test_relative_does_not_fire_after_event_start(harness):
    sched, ctx = harness
    rec = relative_reminder()
    ctx["store"].save(rec)
    ctx["events"] = [_event()]

    sched.tick(at(2026, 7, 10, 18, 0))  # already past the trip start
    assert ctx["messages"] == []


def test_relative_orphan_is_deleted(harness):
    sched, ctx = harness
    rec = relative_reminder(event_id=99)
    ctx["store"].save(rec)
    ctx["events"] = [_event(event_id=42)]  # 99 is gone

    sched.tick(at(2026, 7, 7, 8, 1))
    assert ctx["store"].load(rec["schedule_id"]) is None


def test_relative_survives_event_fetch_failure(harness):
    sched, ctx = harness
    rec = relative_reminder()
    ctx["store"].save(rec)
    ctx["events_fail"] = True  # transient backend outage

    sched.tick(at(2026, 7, 7, 8, 1))
    # Not fired, and crucially NOT deleted as a false orphan.
    assert ctx["messages"] == []
    assert ctx["store"].load(rec["schedule_id"]) is not None


# ── robustness ──────────────────────────────────────────────────────────────

def test_background_unavailable_user_skipped(tmp_path):
    auth = FakeAuth([1], unavailable=[1])
    runs = []
    sched = Scheduler(
        auth=auth,
        schedules_dir=lambda uid: str(tmp_path / str(uid)),
        run_agent=lambda *a: runs.append(a),
        send_message=lambda *a: None,
        upcoming_events=lambda uid: [],
    )
    sched.tick(at(2026, 6, 3, 7, 1))  # must not raise
    assert runs == []
