"""Display formatting for dates and times the model writes back to the user.

The backend stores dates as ``DD/MM/YYYY`` and times as 24-hour ``HH:MM`` —
that wire format never changes (the tool schemas and serve.cpp depend on it).
What *does* vary is how the user wants to *read* dates and times, set in the
web settings ("Date format" / "Time format"). Those preferences live in the
browser and are forwarded to the server on each ``/send`` (see web_app), then
surfaced to the model in the per-turn ``<clock>`` block so it writes prose,
event/topic summaries, and messages in the user's own format.

This module is the single Python implementation of that rendering, mirroring
the client's ``formatDateParts`` / ``formatBackendTime`` in web_chat.html so the
date the model writes matches exactly what the UI shows. Pure functions, no IO.
"""

from __future__ import annotations

import datetime

# The concrete patterns the settings UI offers (its "auto" option is resolved
# to one of these client-side before it ever reaches us). Anything outside this
# set is ignored by callers, falling back to the unambiguous default.
DATE_PATTERNS: frozenset[str] = frozenset({
    "MM/DD/YYYY", "DD/MM/YYYY", "YYYY-MM-DD",
    "MM/DD/YY", "DD/MM/YY", "D MMM YYYY", "MMM D, YYYY",
})
TIME_FORMATS: frozenset[str] = frozenset({"12", "24"})

DEFAULT_DATE_PATTERN = "DD/MM/YYYY"
DEFAULT_TIME_FORMAT = "24"

_MONTH_ABBR = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
               "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def default_date_format(tz: str | None) -> str:
    """A sensible date pattern when the user has no explicit preference and no
    live client is resolving "auto" for us (chiefly background agent runs).

    Derived from the IANA timezone: the Americas conventionally write
    month-first, almost everywhere else day-first. Only a fallback — an explicit
    preference always wins."""
    if tz and tz.split("/", 1)[0] == "America":
        return "MM/DD/YYYY"
    return DEFAULT_DATE_PATTERN


def render_date(d: datetime.date, pattern: str | None) -> str:
    """Render ``d`` in the user's chosen ``pattern`` (one of DATE_PATTERNS).
    An unknown/None pattern falls back to the unambiguous default."""
    day, month, year = d.day, d.month, d.year
    dd = f"{day:02d}"
    mm = f"{month:02d}"
    yyyy = f"{year:04d}"
    yy = yyyy[-2:]
    mon = _MONTH_ABBR[month - 1]
    if pattern == "MM/DD/YYYY":
        return f"{mm}/{dd}/{yyyy}"
    if pattern == "DD/MM/YYYY":
        return f"{dd}/{mm}/{yyyy}"
    if pattern == "YYYY-MM-DD":
        return f"{yyyy}-{mm}-{dd}"
    if pattern == "MM/DD/YY":
        return f"{mm}/{dd}/{yy}"
    if pattern == "DD/MM/YY":
        return f"{dd}/{mm}/{yy}"
    if pattern == "D MMM YYYY":
        return f"{day} {mon} {yyyy}"
    if pattern == "MMM D, YYYY":
        return f"{mon} {day}, {yyyy}"
    return f"{dd}/{mm}/{yyyy}"


def render_time(t: datetime.time, fmt: str | None) -> str:
    """Render ``t`` as 12- or 24-hour per ``fmt`` ('12'/'24'). Unknown/None
    falls back to 24-hour."""
    if fmt == "12":
        suffix = "PM" if t.hour >= 12 else "AM"
        hour = t.hour % 12 or 12
        return f"{hour}:{t.minute:02d} {suffix}"
    return f"{t.hour:02d}:{t.minute:02d}"
