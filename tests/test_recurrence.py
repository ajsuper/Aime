"""Unit tests for aime.scheduling.recurrence — the scheduler's timing math.

These pin the firing semantics the tick loop relies on: next-slot (never
retroactive) recurring fires, day-of-month clamping, the wall-clock-pinned
relative reminder that survives a reschedule, and DST-correct local times.
"""

import datetime
import zoneinfo

import pytest

from aime.scheduling import recurrence as r

TZ = "America/New_York"
ZONE = zoneinfo.ZoneInfo(TZ)


def local(y, mo, d, h=0, mi=0):
    return datetime.datetime(y, mo, d, h, mi, tzinfo=ZONE)


# ── combine_local / DST ─────────────────────────────────────────────────────

def test_combine_local_is_aware_in_zone():
    dt = r.combine_local(datetime.date(2026, 6, 3), datetime.time(7, 0), TZ)
    assert dt == local(2026, 6, 3, 7, 0)
    assert dt.tzinfo is not None


def test_combine_local_tracks_dst_offset():
    # Same wall-clock 09:00, opposite sides of the DST line → different offsets.
    winter = r.combine_local(datetime.date(2026, 1, 15), datetime.time(9, 0), TZ)
    summer = r.combine_local(datetime.date(2026, 7, 15), datetime.time(9, 0), TZ)
    assert winter.tzname() == "EST" and winter.utcoffset() == datetime.timedelta(hours=-5)
    assert summer.tzname() == "EDT" and summer.utcoffset() == datetime.timedelta(hours=-4)


def test_invalid_tz_falls_back_to_utc():
    dt = r.combine_local(datetime.date(2026, 6, 3), datetime.time(7, 0), "Not/AZone")
    assert dt.utcoffset() == datetime.timedelta(0)


# ── recurring: daily ────────────────────────────────────────────────────────

def test_daily_slot_already_passed_rolls_to_tomorrow():
    nxt = r.next_occurrence({"freq": "daily", "time": "07:00"},
                            after="2026-06-03T09:00:00", tz=TZ)
    assert nxt == local(2026, 6, 4, 7, 0)


def test_daily_slot_still_ahead_today_fires_today():
    nxt = r.next_occurrence({"freq": "daily", "time": "07:00"},
                            after="2026-06-03T05:00:00", tz=TZ)
    assert nxt == local(2026, 6, 3, 7, 0)


def test_none_after_is_strictly_in_the_future():
    # A freshly created schedule (no last_run_at) must never fire retroactively.
    nxt = r.next_occurrence({"freq": "daily", "time": "07:00"}, after=None, tz=TZ)
    assert nxt > datetime.datetime.now(ZONE)


# ── recurring: weekly ───────────────────────────────────────────────────────

def test_weekly_finds_next_target_weekday():
    # 2026-06-03 is a Wednesday; next Monday (weekday=1) is the 8th.
    nxt = r.next_occurrence({"freq": "weekly", "weekday": 1, "time": "09:00"},
                            after="2026-06-03T12:00:00", tz=TZ)
    assert nxt == local(2026, 6, 8, 9, 0)


def test_weekly_same_day_time_passed_rolls_a_week():
    # 2026-06-08 is a Monday; 09:00 already gone → next Monday is the 15th.
    nxt = r.next_occurrence({"freq": "weekly", "weekday": 1, "time": "09:00"},
                            after="2026-06-08T10:00:00", tz=TZ)
    assert nxt == local(2026, 6, 15, 9, 0)


# ── recurring: monthly ──────────────────────────────────────────────────────

def test_monthly_clamps_day_to_month_end():
    # day=31 in February (2026, non-leap) clamps to the 28th.
    nxt = r.next_occurrence({"freq": "monthly", "day": 31, "time": "08:00"},
                            after="2026-02-10T00:00:00", tz=TZ)
    assert nxt == local(2026, 2, 28, 8, 0)


def test_monthly_advances_to_next_month_when_passed():
    nxt = r.next_occurrence({"freq": "monthly", "day": 31, "time": "08:00"},
                            after="2026-02-28T09:00:00", tz=TZ)
    assert nxt == local(2026, 3, 31, 8, 0)


def test_unknown_freq_raises():
    with pytest.raises(ValueError):
        r.next_occurrence({"freq": "hourly", "time": "08:00"}, after=None, tz=TZ)


# ── once ────────────────────────────────────────────────────────────────────

def test_once_due_is_the_absolute_local_instant():
    assert r.once_due({"at": "2026-06-10T15:00:00"}, TZ) == local(2026, 6, 10, 15, 0)


# ── relative (event-anchored reminders) ─────────────────────────────────────

def test_parse_event_start():
    assert r.parse_event_start("10/07/2026", "14:00", TZ) == local(2026, 7, 10, 14, 0)


def test_relative_pins_wall_clock_independent_of_start_time():
    # "8am, three days before" a trip departing 14:00 → 08:00 on the 7th,
    # NOT 14:00 minus three days.
    start = r.parse_event_start("10/07/2026", "14:00", TZ)
    due = r.relative_due(start, {"days_before": 3, "at_time": "08:00",
                                 "minutes_before": 0}, TZ)
    assert due == local(2026, 7, 7, 8, 0)


def test_relative_reschedule_keeps_the_pinned_time():
    # Move the trip to the 12th at 10:00; the 8am/3-days rule re-derives to the
    # 9th at 08:00 with no stored offset to drift.
    moved = r.parse_event_start("12/07/2026", "10:00", TZ)
    due = r.relative_due(moved, {"days_before": 3, "at_time": "08:00",
                                 "minutes_before": 0}, TZ)
    assert due == local(2026, 7, 9, 8, 0)


def test_relative_null_time_inherits_start_and_applies_minutes():
    # "15 minutes before" → floats with the start instant.
    start = r.parse_event_start("10/07/2026", "14:00", TZ)
    due = r.relative_due(start, {"days_before": 0, "at_time": None,
                                 "minutes_before": 15}, TZ)
    assert due == local(2026, 7, 10, 13, 45)
