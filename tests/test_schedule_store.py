"""Unit tests for aime.scheduling.store — encrypted CRUD + invariant validation.

Covers the round-trip (encrypt → list → load → delete), that the on-disk blob
is actually opaque and AAD-bound to the schedule id, and the tagged-variant
rules that keep a field from ever sitting in a variant that doesn't use it.
"""

import datetime

import pytest

from aime import encryption as enc
from aime.scheduling import store as s

TZ = "America/New_York"


@pytest.fixture
def dek():
    return enc.generate_dek()


def recurring_agent(**over):
    rec = s.make_schedule(
        trigger={"freq": "daily", "time": "07:00", "kind": "recurring"},
        action={"kind": "run_agent", "agent_id": "agent-brief-1234"},
        tz=TZ,
        label="morning brief",
    )
    rec.update(over)
    return rec


def relative_reminder(**over):
    rec = s.make_schedule(
        trigger={"kind": "relative", "event_id": 42, "days_before": 3,
                 "at_time": "08:00", "minutes_before": 0},
        action={"kind": "send_message", "message": "Pack for {event_title}"},
        tz=TZ,
    )
    rec.update(over)
    return rec


# ── make_schedule / new_schedule_id ─────────────────────────────────────────

def test_new_schedule_id_shape_and_uniqueness():
    a = s.new_schedule_id("My Trip!")
    b = s.new_schedule_id("My Trip!")
    assert a.startswith("sch-my-trip-") and a != b


def test_make_schedule_initializes_empty_state():
    rec = recurring_agent()
    assert rec["state"] == {"last_run_at": None, "fired_at": None, "sent_for_start": None}
    assert rec["enabled"] is True
    assert rec["created_at"] and rec["updated_at"]
    assert s.validate_schedule(rec) is None


# ── encrypted round-trip ────────────────────────────────────────────────────

def test_save_load_round_trip(tmp_path, dek):
    store = s.ScheduleStore(str(tmp_path), dek)
    rec = recurring_agent()
    assert store.save(rec) is True
    loaded = store.load(rec["schedule_id"])
    assert loaded["trigger"] == rec["trigger"]
    assert loaded["action"] == rec["action"]


def test_blob_on_disk_is_opaque(tmp_path, dek):
    store = s.ScheduleStore(str(tmp_path), dek)
    rec = relative_reminder()
    store.save(rec)
    blob = (tmp_path / f"{rec['schedule_id']}{s._SUFFIX}").read_bytes()
    assert b"event_title" not in blob and b"send_message" not in blob


def test_wrong_id_aad_fails_to_decrypt(tmp_path, dek):
    # The schedule_id is bound in as AEAD associated data, so a file read under
    # a different id won't authenticate.
    store = s.ScheduleStore(str(tmp_path), dek)
    rec = recurring_agent()
    store.save(rec)
    src = tmp_path / f"{rec['schedule_id']}{s._SUFFIX}"
    (tmp_path / f"sch-tampered{s._SUFFIX}").write_bytes(src.read_bytes())
    assert store.load("sch-tampered") is None


def test_list_newest_first_and_delete(tmp_path, dek):
    store = s.ScheduleStore(str(tmp_path), dek)
    older = recurring_agent(created_at="2026-06-01T00:00:00+00:00")
    newer = relative_reminder(created_at="2026-06-02T00:00:00+00:00")
    store.save(older)
    store.save(newer)
    ids = [r["schedule_id"] for r in store.list_schedules()]
    assert ids == [newer["schedule_id"], older["schedule_id"]]

    assert store.delete(newer["schedule_id"]) is True
    assert store.delete(newer["schedule_id"]) is False
    assert len(store.list_schedules()) == 1


def test_list_skips_unreadable_files(tmp_path, dek):
    store = s.ScheduleStore(str(tmp_path), dek)
    store.save(recurring_agent())
    (tmp_path / f"sch-garbage{s._SUFFIX}").write_bytes(b"not a real blob")
    assert len(store.list_schedules()) == 1  # the garbage file is silently skipped


# ── validation invariants ───────────────────────────────────────────────────

def test_save_rejects_invalid_record(tmp_path, dek):
    store = s.ScheduleStore(str(tmp_path), dek)
    bad = recurring_agent()
    bad["trigger"]["freq"] = "hourly"
    assert store.save(bad) is False
    assert store.load(bad["schedule_id"]) is None


def test_relative_requires_event_id():
    rec = relative_reminder()
    rec["trigger"].pop("event_id")
    assert s.validate_schedule(rec) == "relative trigger requires event_id"


def test_non_relative_must_not_set_event_id():
    rec = recurring_agent()
    rec["trigger"]["event_id"] = 7
    assert "must not set event_id" in s.validate_schedule(rec)


def test_weekly_requires_weekday():
    rec = recurring_agent()
    rec["trigger"].update(freq="weekly", time="09:00")
    assert "weekday" in s.validate_schedule(rec)
    rec["trigger"]["weekday"] = 1
    assert s.validate_schedule(rec) is None


def test_monthly_requires_day():
    rec = recurring_agent()
    rec["trigger"].update(freq="monthly", time="09:00")
    assert "day" in s.validate_schedule(rec)
    rec["trigger"]["day"] = 15
    assert s.validate_schedule(rec) is None


def test_days_before_bounds():
    rec = relative_reminder()
    rec["trigger"]["days_before"] = 400
    assert "days_before" in s.validate_schedule(rec)


def test_at_time_format():
    rec = relative_reminder()
    rec["trigger"]["at_time"] = "8am"
    assert "at_time" in s.validate_schedule(rec)
    rec["trigger"]["at_time"] = None  # null is allowed (inherit event start)
    assert s.validate_schedule(rec) is None


def test_action_invariants():
    run = recurring_agent()
    run["action"] = {"kind": "run_agent"}
    assert "agent_id" in s.validate_schedule(run)

    msg = relative_reminder()
    msg["action"] = {"kind": "send_message", "message": "hi", "agent_id": "x"}
    assert "must not set agent_id" in s.validate_schedule(msg)


def test_bad_timezone_rejected():
    rec = recurring_agent(tz="Not/AZone")
    assert s.validate_schedule(rec) == "tz must be a valid IANA timezone"


def test_once_requires_parseable_at():
    rec = s.make_schedule(
        trigger={"kind": "once", "at": "not-a-date"},
        action={"kind": "send_message", "message": "ping"},
        tz=TZ,
    )
    assert "ISO datetime" in s.validate_schedule(rec)
    rec["trigger"]["at"] = "2026-06-10T15:00:00"
    assert s.validate_schedule(rec) is None
