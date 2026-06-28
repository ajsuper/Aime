"""Active-event context for the model.

On the first message of a chat we hand the model a `<active_events>` block: a
snapshot of the events happening *right now*, so it never has to guess (or
query) what's currently going on. It's point-in-time metadata, tied to the
moment the user opened the conversation — much like the per-turn `<clock>`.

An event counts as **active** when the current wall-clock moment falls inside
its span:
  * a timed event with an end is active between its start and end;
  * an all-day or multi-day event is active for the whole day(s) it covers;
  * a point event (a time but no end, or no time at all) has no real duration,
    so we treat it as a whole-day event — active all of its day. That keeps
    open-ended but important items ("Pick up brother from airport") in front of
    the model rather than dropping them for lacking an end.
Only live events count — `scheduled` and not archived. Completed/canceled ones
are history, not happening now.

The fetch deliberately reaches far back (a long-running trip may have started
weeks ago) but only the active subset is rendered, so breadth costs nothing in
the model's context.
"""

import datetime

from .services import _events_from

_DATE_FMT = "%d/%m/%Y"
_TIME_FMT = "%H:%M"
# How far back to look for still-running multi-day events. Generous on purpose:
# only active events are surfaced, so a wide window doesn't bloat model context.
_LOOKBACK_DAYS = 400
_SUMMARY_MAX = 140
# Cap how many active events ride the first message, so a packed day can't bloat
# every chat's opening context. Overflow is summarized with a count.
_MAX_EVENTS = 15


def _parse_dt(date_s, time_s, *, end_of_day=False):
    """Combine a DD/MM/YYYY date and optional HH:MM time into a naive wall-clock
    datetime. A missing/blank time anchors to start- or end-of-day."""
    try:
        d = datetime.datetime.strptime((date_s or "").strip(), _DATE_FMT).date()
    except ValueError:
        return None
    t = (time_s or "").strip()
    tm = None
    if t:
        try:
            tm = datetime.datetime.strptime(t, _TIME_FMT).time()
        except ValueError:
            tm = None
    if tm is None:
        tm = datetime.time(23, 59) if end_of_day else datetime.time(0, 0)
    return datetime.datetime.combine(d, tm)


def event_span(ev):
    """(start, end) naive wall-clock span of an event, or None when its date is
    unparseable. An event with a recorded end uses that precise span; one without
    (an all-day item OR a point event with a nominal time) spans its whole day."""
    date_s = ev.get("date")
    if not date_s:
        return None
    time_s = (ev.get("time") or "").strip()
    end_date_s = (ev.get("end_date") or "").strip()
    end_time_s = (ev.get("end_time") or "").strip()
    if end_date_s:
        start = _parse_dt(date_s, time_s)
        # A multi-day timed event with no end time runs to the end of its end day.
        end = _parse_dt(end_date_s, end_time_s, end_of_day=not end_time_s)
        return (start, end) if start and end else None
    # No recorded end → whole-day span (covers all-day items and point events
    # with a nominal start time but no duration).
    start = _parse_dt(date_s, "")
    end = _parse_dt(date_s, "", end_of_day=True)
    return (start, end) if start and end else None


def _is_live(ev):
    if ev.get("archived"):
        return False
    status = (ev.get("status") or "scheduled").strip() or "scheduled"
    return status == "scheduled"


def active_events(events, now):
    """List of (event, span) for events whose span contains `now`, live only."""
    out = []
    for ev in events:
        if not _is_live(ev):
            continue
        span = event_span(ev)
        if span and span[0] <= now <= span[1]:
            out.append((ev, span))
    out.sort(key=lambda pair: pair[1][0])  # earliest-started first
    return out


def _when_desc(ev, span, now):
    start, end = span
    time_s = (ev.get("time") or "").strip()
    has_end = bool((ev.get("end_date") or "").strip())
    if not has_end:
        # Whole-day event: a point event keeps its nominal time as a hint (it has
        # no duration, so we don't pretend a window); a timeless one is all-day.
        return f"today at {time_s} (no set duration)" if time_s else "all day today"
    same_day = start.date() == end.date()
    if not time_s:
        total = (end.date() - start.date()).days + 1
        day_n = (now.date() - start.date()).days + 1
        return (f"all day · {start.strftime(_DATE_FMT)}→{end.strftime(_DATE_FMT)} "
                f"(day {day_n} of {total})")
    if same_day:
        return f"{start.strftime(_TIME_FMT)}–{end.strftime(_TIME_FMT)} today"
    return (f"{start.strftime(_DATE_FMT)} {start.strftime(_TIME_FMT)} → "
            f"{end.strftime(_DATE_FMT)} {end.strftime(_TIME_FMT)}")


def render_active_events_block(events, now):
    """The `<active_events>` block for the given events at `now`, or '' if none
    are active."""
    active = active_events(events, now)
    if not active:
        return ""
    n = len(active)
    header = (f"{n} event{'s' if n != 1 else ''} active now or on today's agenda "
              f"(as of {now.strftime(_DATE_FMT)} {now.strftime(_TIME_FMT)}):")
    lines = [header]
    for ev, span in active[:_MAX_EVENTS]:
        eid = ev.get("id", "?")
        title = (ev.get("title") or "(untitled)").strip()
        cat = (ev.get("category") or "").strip()
        cat_s = f" ({cat})" if cat else ""
        line = f'- #{eid} "{title}"{cat_s} — {_when_desc(ev, span, now)}'
        summary = " ".join((ev.get("summary") or "").split())
        if summary:
            if len(summary) > _SUMMARY_MAX:
                summary = summary[: _SUMMARY_MAX - 1].rstrip() + "…"
            line += f" — {summary}"
        lines.append(line)
    if n > _MAX_EVENTS:
        lines.append(f"…and {n - _MAX_EVENTS} more (use FilterUsersEvents to see all).")
    return "<active_events>\n" + "\n".join(lines) + "\n</active_events>"


def active_events_prefix(gateway, now):
    """Fetch recent events via the tool gateway and render the `<active_events>`
    block for `now` (naive local wall-clock). Returns '' on any failure or when
    nothing is active — this is best-effort context that must never block a
    message from being sent."""
    try:
        horizon = (now.date() - datetime.timedelta(days=_LOOKBACK_DAYS))
        data = gateway.call(
            "get_events",
            filter_by_date=True,
            start_date=horizon.strftime(_DATE_FMT),
            end_date=now.strftime(_DATE_FMT),
            archived="active_only",
            sort_order="asc",
        )
        if isinstance(data, dict) and data.get("error"):
            return ""
        return render_active_events_block(_events_from(data), now)
    except Exception:
        return ""
