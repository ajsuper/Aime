"""Unit tests for aime.scheduling.reminders — the engine behind the
CreateReminder / ListReminders / DeleteReminder client tools.

Drives a real (tmp, encrypted) ScheduleStore with a canned event list, so these
also exercise that a reminder the model creates is a valid, scheduler-ready
``relative`` + ``send_message`` record — the same shape the event-modal UI writes.
"""

import pytest

from aime import encryption as enc
from aime.scheduling.reminders import (
    ReminderService,
    describe_lead,
    reminder_message_for,
)
from aime.scheduling.store import ScheduleStore, validate_schedule

TZ = "America/New_York"
EVENTS = [
    {"id": 42, "title": "Dentist", "date": "10/06/2026", "time": "09:30"},
    {"id": 7, "title": "All-day offsite", "date": "12/06/2026"},  # no time
]


@pytest.fixture
def svc(tmp_path):
    store = ScheduleStore(str(tmp_path), enc.generate_dek())
    return ReminderService(store, lambda: list(EVENTS), default_tz=TZ)


def test_describe_lead_phrasing():
    assert describe_lead(0, None) == "on the day"
    assert describe_lead(1, None) == "1 day before"
    assert describe_lead(3, "08:00") == "3 days before at 08:00"


def test_message_template_includes_time_only_when_event_has_one():
    assert "{event_time}" in reminder_message_for({"time": "09:30"})
    assert "{event_time}" not in reminder_message_for({})


def test_create_writes_a_valid_scheduler_ready_record(svc):
    res = svc.create(event_id=42, days_before=3, at_time="08:00")
    assert res["ok"] and res["event_title"] == "Dentist"
    assert res["lead"] == "3 days before at 08:00"

    stored = svc._store.load(res["reminder_id"])
    assert validate_schedule(stored) is None        # scheduler would accept it
    assert stored["trigger"]["kind"] == "relative"
    assert stored["trigger"]["event_id"] == 42
    assert stored["action"]["kind"] == "send_message"
    assert stored["tz"] == TZ


def test_create_respects_caller_timezone_over_default(svc):
    res = svc.create(event_id=42, days_before=1, tz="Europe/London")
    assert svc._store.load(res["reminder_id"])["tz"] == "Europe/London"


def test_create_inherits_event_time_when_at_time_omitted(svc):
    res = svc.create(event_id=42, days_before=0)
    assert res["lead"] == "on the day"
    assert svc._store.load(res["reminder_id"])["trigger"]["at_time"] is None


def test_create_rejects_unknown_event(svc):
    res = svc.create(event_id=999, days_before=1)
    assert not res["ok"] and "999" in res["error"]
    assert svc.list() == []


def test_create_tolerates_string_event_id(svc):
    # The model may echo the id back as a string; it must still link to int 42.
    res = svc.create(event_id="42", days_before=1)
    assert res["ok"]
    assert svc._store.load(res["reminder_id"])["trigger"]["event_id"] == 42


def test_list_filters_by_event_and_flattens(svc):
    svc.create(event_id=42, days_before=3, at_time="08:00")
    svc.create(event_id=7, days_before=1)
    assert len(svc.list()) == 2
    only_42 = svc.list(event_id=42)
    assert len(only_42) == 1
    row = only_42[0]
    assert row["event_title"] == "Dentist"
    assert row["days_before"] == 3 and row["at_time"] == "08:00"
    assert row["lead"] == "3 days before at 08:00"


def test_delete_removes_only_the_named_reminder(svc):
    a = svc.create(event_id=42, days_before=3)["reminder_id"]
    b = svc.create(event_id=7, days_before=1)["reminder_id"]
    assert svc.delete(a)["ok"]
    remaining = {r["reminder_id"] for r in svc.list()}
    assert remaining == {b}


def test_delete_unknown_id_is_a_clean_failure(svc):
    res = svc.delete("sch-nope-00000000")
    assert not res["ok"] and res["error"]


def test_delete_refuses_non_reminder_records(svc, tmp_path):
    # A run_agent (scheduled-agent) record must not be deletable via this tool.
    from aime.scheduling.store import make_schedule
    agent_rec = make_schedule(
        trigger={"kind": "recurring", "freq": "daily", "time": "07:00"},
        action={"kind": "run_agent", "agent_id": "agent-x-1234"},
        tz=TZ,
    )
    svc._store.save(agent_rec)
    res = svc.delete(agent_rec["schedule_id"])
    assert not res["ok"] and "reminder" in res["error"]
    assert svc._store.load(agent_rec["schedule_id"]) is not None  # untouched
