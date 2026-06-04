"""Pure recurrence/timing math for the scheduler — no IO, no state.

Every "when does this fire" decision lives here so neither the model nor the
tick loop ever does date arithmetic. The functions are total and deterministic:
given a trigger and a reference instant they return a single timezone-aware
``datetime`` in the user's zone. The scheduler then compares that against "now"
and the grace window to decide whether to fire.

Three trigger kinds, three entry points:

* ``recurring`` → :func:`next_occurrence` — daily/weekly/monthly cron-style.
* ``once``      → :func:`once_due`       — a fixed absolute local instant.
* ``relative``  → :func:`relative_due`   — anchored to a linked event's start,
  recomputed live each tick so a rescheduled event drags its reminder along.

All wall-clock times are interpreted in the supplied IANA ``tz`` via
``zoneinfo``, so "08:00" means 08:00 local across DST boundaries. The backend
stores event date/time as ``DD/MM/YYYY`` + ``HH:MM``; :func:`parse_event_start`
turns that pair into the aware datetime :func:`relative_due` expects.
"""

from __future__ import annotations

import calendar
import datetime
import zoneinfo


def _zone(tz: str | None) -> zoneinfo.ZoneInfo:
    """The user's zone, falling back to UTC for a missing/invalid name so a bad
    stored tz degrades to a sane instant rather than raising in the tick loop."""
    try:
        return zoneinfo.ZoneInfo(tz) if tz else zoneinfo.ZoneInfo("UTC")
    except Exception:
        return zoneinfo.ZoneInfo("UTC")


def combine_local(
    date: datetime.date, time: datetime.time, tz: str | None
) -> datetime.datetime:
    """A timezone-aware datetime at local ``date``/``time`` in ``tz``.

    ``time``'s own tzinfo (if any) is dropped — the zone always comes from
    ``tz`` — so callers can pass a naive or an aware time interchangeably."""
    naive = datetime.datetime.combine(date, time.replace(tzinfo=None))
    return naive.replace(tzinfo=_zone(tz))


def _parse_local(value: str | None, tz: str | None) -> datetime.datetime:
    """Parse an ISO string into an aware datetime in ``tz`` (now if ``None``).

    A naive string is read as already-local; an offset-bearing string is
    converted into ``tz`` so all downstream comparisons share one zone."""
    zone = _zone(tz)
    if not value:
        return datetime.datetime.now(zone)
    dt = datetime.datetime.fromisoformat(value)
    if dt.tzinfo is None:
        return dt.replace(tzinfo=zone)
    return dt.astimezone(zone)


def _hh_mm(value: str | None, default: datetime.time) -> datetime.time:
    """Parse ``"HH:MM"`` into a time, falling back to ``default`` if absent."""
    if not value:
        return default
    hh, mm = value.split(":")
    return datetime.time(int(hh), int(mm))


def parse_event_start(
    date_str: str, time_str: str | None, tz: str | None
) -> datetime.datetime:
    """Turn the backend's ``DD/MM/YYYY`` + ``HH:MM`` event fields into an aware
    local datetime. A missing/blank time is treated as midnight."""
    day, month, year = (int(p) for p in date_str.split("/"))
    t = _hh_mm(time_str, datetime.time(0, 0))
    return combine_local(datetime.date(year, month, day), t, tz)


def next_occurrence(
    trigger: dict, *, after: str | None, tz: str | None
) -> datetime.datetime:
    """The next ``recurring`` fire strictly after ``after`` (or now if ``None``).

    ``after`` is the schedule's ``last_run_at`` — so a freshly created schedule
    (no last run) first fires at its next slot, never retroactively. Handles
    daily/weekly/monthly, day-of-month clamping to the month's end, and DST via
    ``zoneinfo``."""
    ref = _parse_local(after, tz)
    fire_time = _hh_mm(trigger.get("time"), datetime.time(0, 0))
    freq = trigger.get("freq")

    if freq == "daily":
        cand = combine_local(ref.date(), fire_time, tz)
        if cand <= ref:
            cand += datetime.timedelta(days=1)
        return cand

    if freq == "weekly":
        # Stored weekday is 1-7 (Mon-Sun); Python's weekday() is 0-6 (Mon-Sun).
        target = int(trigger.get("weekday", 1)) - 1
        days_ahead = (target - ref.weekday()) % 7
        cand = combine_local(
            ref.date() + datetime.timedelta(days=days_ahead), fire_time, tz
        )
        if cand <= ref:  # same weekday, but the time today already passed
            cand += datetime.timedelta(days=7)
        return cand

    if freq == "monthly":
        day = int(trigger.get("day", 1))
        cand = _monthly_candidate(ref.year, ref.month, day, fire_time, tz)
        if cand <= ref:
            year, month = (ref.year + 1, 1) if ref.month == 12 else (ref.year, ref.month + 1)
            cand = _monthly_candidate(year, month, day, fire_time, tz)
        return cand

    raise ValueError(f"unknown recurring freq: {freq!r}")


def _monthly_candidate(
    year: int, month: int, day: int, fire_time: datetime.time, tz: str | None
) -> datetime.datetime:
    """A fire instant on ``day`` of ``year``/``month``, clamped to the month's
    last day so e.g. ``day=31`` lands on Feb 28/29."""
    last = calendar.monthrange(year, month)[1]
    return combine_local(datetime.date(year, month, min(day, last)), fire_time, tz)


def once_due(trigger: dict, tz: str | None) -> datetime.datetime:
    """The fire instant for a ``once`` trigger: its absolute local ``at`` time."""
    return _parse_local(trigger.get("at"), tz)


def relative_due(
    event_start: datetime.datetime, trigger: dict, tz: str | None
) -> datetime.datetime:
    """The fire instant for a ``relative`` trigger, anchored to ``event_start``.

    ``days_before`` shifts the event's *date* back; ``at_time`` pins a wall-clock
    time on that day (``None`` inherits the event's own start time, so the
    reminder floats with the start); ``minutes_before`` applies a final fine
    delta. Recomputed every tick against the live event, so moving the event
    moves the reminder with no stored offset to drift."""
    days_before = int(trigger.get("days_before", 0))
    minutes_before = int(trigger.get("minutes_before", 0))
    base_date = event_start.date() - datetime.timedelta(days=days_before)
    base_time = _hh_mm(trigger.get("at_time"), event_start.timetz())
    due = combine_local(base_date, base_time, tz)
    return due - datetime.timedelta(minutes=minutes_before)
