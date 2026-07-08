"""Event-length normalization: turn a `duration` input into an absolute end.

Aime stores an event's length as an **absolute** end (`end_date` + optional
`end_time`), mirroring the start's shape — see the C++ `CalenderEvent`. A
`duration` is only ever an authoring convenience the model may pass on
create/edit; we resolve it to a concrete end *here*, in the Python tool layer,
before the request reaches the backend. That keeps a single source of truth
(the absolute end) and means the C++ side never does calendar math.

Why resolve duration to an absolute end rather than store it? Because the two
differ the moment an event moves: an absolute end stays put (predictable), a
duration would slide. By normalizing at write time, "move + keep length" stays
an explicit choice — the model re-sends `duration` to slide the end, or repeats
the existing end to anchor it.

The duration grammar is compact and unit-tagged so long spans need no math from
the model: e.g. ``90m``, ``2h``, ``1h30m``, ``3d``, ``2w``, ``2mo``, ``1y``.
Sub-day units (h/m) require a start time; day-and-up units work on all-day
events too. Months/years use calendar-aware arithmetic (``relativedelta``).
"""

import calendar
import datetime
import re

# Unit token -> how to apply it. mo/y are calendar-aware (whole-month steps with
# end-of-month clamping); the rest are fixed spans (timedelta). `sub_day` flags
# the units that only make sense once the event has a start time.
_UNIT_RE = re.compile(r"(\d+)\s*(mo|y|w|d|h|m)")

_DATE_FMT = "%d/%m/%Y"
_TIME_FMT = "%H:%M"


class EventLengthError(ValueError):
    """Raised when a duration / end pair can't be resolved into a valid end.

    Message is user/model-facing — kept short and concrete so the model can
    correct itself or explain the problem to the user."""


def _add_months(d: datetime.date, months: int) -> datetime.date:
    """Add whole calendar months to a date, clamping the day to the target
    month's length (so 31 Jan + 1mo = 28/29 Feb, matching every calendar app)."""
    total = d.month - 1 + months
    year = d.year + total // 12
    month = total % 12 + 1
    day = min(d.day, calendar.monthrange(year, month)[1])
    return datetime.date(year, month, day)


def _parse_duration(raw) -> tuple[int, datetime.timedelta, bool]:
    """Parse a duration into (months, fixed_delta, has_sub_day).

    `months` carries the calendar-aware part (mo/y); `fixed_delta` the exact
    span (w/d/h/m). Accepts a bare number (minutes) or a unit-tagged string.
    Raises EventLengthError on anything unparseable or non-positive."""
    if isinstance(raw, (int, float)):
        minutes = int(raw)
        if minutes <= 0:
            raise EventLengthError("duration must be a positive amount of time.")
        return 0, datetime.timedelta(minutes=minutes), True

    text = str(raw).strip().lower()
    if not text:
        raise EventLengthError("duration is empty.")
    # A bare number with no unit means minutes (matches the int form above).
    if text.isdigit():
        return _parse_duration(int(text))

    matches = _UNIT_RE.findall(text)
    # Reject junk: the matched spans must cover the whole string (modulo spaces).
    if not matches or _UNIT_RE.sub("", text).strip():
        raise EventLengthError(
            "duration must look like '90m', '2h', '1h30m', '3d', '2w', '2mo', or '1y'."
        )

    months = 0
    fixed = datetime.timedelta()
    has_sub_day = False
    for amount, unit in matches:
        n = int(amount)
        if unit == "mo":
            months += n
        elif unit == "y":
            months += 12 * n
        elif unit == "w":
            fixed += datetime.timedelta(weeks=n)
        elif unit == "d":
            fixed += datetime.timedelta(days=n)
        elif unit == "h":
            fixed += datetime.timedelta(hours=n)
            has_sub_day = True
        elif unit == "m":
            fixed += datetime.timedelta(minutes=n)
            has_sub_day = True
    if months == 0 and fixed == datetime.timedelta():
        raise EventLengthError("duration must be a positive amount of time.")
    return months, fixed, has_sub_day


def _parse_date(s: str) -> datetime.date:
    try:
        return datetime.datetime.strptime(s.strip(), _DATE_FMT).date()
    except ValueError:
        raise EventLengthError(f"date '{s}' is not DD/MM/YYYY.")


def _parse_time(s: str) -> datetime.time:
    try:
        return datetime.datetime.strptime(s.strip(), _TIME_FMT).time()
    except ValueError:
        raise EventLengthError(f"time '{s}' is not HH:MM.")


def normalize_event_length(payload: dict) -> dict:
    """Resolve a create/replace-event payload's length into absolute end fields.

    Returns a NEW dict: `duration` (if any) is consumed and replaced with the
    computed `end_date`/`end_time`; an explicit end is validated and passed
    through. Raises EventLengthError on an inconsistent or invalid combination.

    Rules:
      * Length is optional — a payload with neither `duration` nor an end is a
        point-in-time event and passes through untouched.
      * `duration` and an explicit `end_date`/`end_time` are mutually exclusive
        (the duration grammar is the sugar; the end is the canonical form).
      * Sub-day durations (hours/minutes) require a start `time`.
      * An `end_time` requires a start `time` (a timed end needs a timed start).
      * The end must not fall before the start.
    """
    out = dict(payload)
    duration = out.pop("duration", None)
    has_duration = duration not in (None, "")
    end_date = (out.get("end_date") or "").strip()
    end_time = (out.get("end_time") or "").strip()
    has_explicit_end = bool(end_date or end_time)

    if has_duration and has_explicit_end:
        raise EventLengthError(
            "set either duration or end_date/end_time, not both."
        )

    # No length given at all → leave the event a point in time.
    if not has_duration and not has_explicit_end:
        return out

    date_str = (out.get("date") or "").strip()
    if not date_str:
        raise EventLengthError("an event needs a start date before it can have a length.")
    start_date = _parse_date(date_str)
    time_str = (out.get("time") or "").strip()
    start_time = _parse_time(time_str) if time_str else None

    if has_duration:
        months, fixed, has_sub_day = _parse_duration(duration)
        if has_sub_day and start_time is None:
            raise EventLengthError(
                "a duration in hours/minutes needs a start time on the event."
            )
        # Apply the calendar-aware months first, then the exact span.
        if start_time is not None:
            start_dt = datetime.datetime.combine(_add_months(start_date, months), start_time)
            end_dt = start_dt + fixed
            out["end_date"] = end_dt.date().strftime(_DATE_FMT)
            out["end_time"] = end_dt.time().strftime(_TIME_FMT)
        else:
            # All-day event: add whole-day spans to the date, no end time.
            end_d = _add_months(start_date, months) + fixed
            out["end_date"] = end_d.strftime(_DATE_FMT)
            out["end_time"] = ""
        return out

    # Explicit end. Validate shape and ordering.
    if end_time and start_time is None:
        raise EventLengthError("an end time needs the event to have a start time.")
    end_d = _parse_date(end_date) if end_date else start_date
    if end_time:
        end_dt = datetime.datetime.combine(end_d, _parse_time(end_time))
        start_dt = datetime.datetime.combine(start_date, start_time)
        if end_dt < start_dt:
            raise EventLengthError("the event's end is before its start.")
    elif end_d < start_date:
        raise EventLengthError("the event's end date is before its start date.")
    out["end_date"] = end_date or start_date.strftime(_DATE_FMT)
    out["end_time"] = end_time
    return out
