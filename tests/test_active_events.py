"""Unit tests for aime.active_events — the first-message <active_events> snapshot.

These pin which events count as "happening now" (timed spans, all-day, multi-day,
the point-event exclusion), the live-only filter, and the rendered block shape.
"""

import datetime

import pytest

from aime.active_events import (
    event_span,
    active_events,
    render_active_events_block,
    active_events_prefix,
)


def ev(**kw):
    base = {"id": 1, "title": "X", "date": "", "time": "", "end_date": "",
            "end_time": "", "category": "", "summary": "", "archived": False,
            "status": "scheduled"}
    base.update(kw)
    return base


NOW = datetime.datetime(2026, 6, 10, 14, 30)   # 10/06/2026 14:30


# --- span computation --------------------------------------------------------

def test_same_day_timed_span():
    s = event_span(ev(date="10/06/2026", time="14:00", end_date="10/06/2026", end_time="15:30"))
    assert s == (datetime.datetime(2026, 6, 10, 14, 0),
                 datetime.datetime(2026, 6, 10, 15, 30))


def test_all_day_single_span_is_whole_day():
    s = event_span(ev(date="10/06/2026"))
    assert s[0] == datetime.datetime(2026, 6, 10, 0, 0)
    assert s[1] == datetime.datetime(2026, 6, 10, 23, 59)


def test_multi_day_all_day_span():
    s = event_span(ev(date="09/06/2026", end_date="11/06/2026"))
    assert s[0].date() == datetime.date(2026, 6, 9)
    assert s[1] == datetime.datetime(2026, 6, 11, 23, 59)


def test_multi_day_timed_no_end_time_runs_to_end_of_day():
    s = event_span(ev(date="09/06/2026", time="18:00", end_date="11/06/2026"))
    assert s[1] == datetime.datetime(2026, 6, 11, 23, 59)


def test_point_event_has_no_span():
    assert event_span(ev(date="10/06/2026", time="14:00")) is None


def test_dateless_event_has_no_span():
    assert event_span(ev(date="")) is None


# --- active filtering --------------------------------------------------------

def test_active_includes_current_timed_event():
    e = ev(id=42, date="10/06/2026", time="14:00", end_date="10/06/2026", end_time="15:30")
    assert [x[0]["id"] for x in active_events([e], NOW)] == [42]


def test_inactive_when_now_past_end():
    e = ev(date="10/06/2026", time="10:00", end_date="10/06/2026", end_time="11:00")
    assert active_events([e], NOW) == []


def test_active_includes_todays_all_day():
    assert len(active_events([ev(date="10/06/2026")], NOW)) == 1


def test_active_includes_multi_day_in_progress():
    assert len(active_events([ev(date="08/06/2026", end_date="12/06/2026")], NOW)) == 1


def test_point_event_never_active():
    assert active_events([ev(date="10/06/2026", time="14:30")], NOW) == []


def test_archived_excluded():
    e = ev(date="10/06/2026", end_date="10/06/2026", archived=True)
    assert active_events([e], NOW) == []


def test_canceled_and_completed_excluded():
    for st in ("canceled", "completed", "unknown"):
        e = ev(date="08/06/2026", end_date="12/06/2026", status=st)
        assert active_events([e], NOW) == [], st


def test_sorted_by_start():
    a = ev(id=1, date="10/06/2026", time="14:00", end_date="10/06/2026", end_time="16:00")
    b = ev(id=2, date="08/06/2026", end_date="12/06/2026")  # started earlier
    ids = [x[0]["id"] for x in active_events([a, b], NOW)]
    assert ids == [2, 1]


# --- rendering ---------------------------------------------------------------

def test_render_empty_when_nothing_active():
    assert render_active_events_block([ev(date="01/01/2020")], NOW) == ""


def test_render_block_shape_and_details():
    e = ev(id=7, title="Sprint Demo", category="work", summary="Show the build to the team",
           date="10/06/2026", time="14:00", end_date="10/06/2026", end_time="15:30")
    block = render_active_events_block([e], NOW)
    assert block.startswith("<active_events>\n")
    assert block.endswith("\n</active_events>")
    assert "1 event happening right now" in block
    assert '#7 "Sprint Demo" (work)' in block
    assert "14:00–15:30 today" in block
    assert "Show the build to the team" in block


def test_render_multiday_shows_day_count():
    e = ev(id=9, title="Conf", date="08/06/2026", end_date="12/06/2026")
    block = render_active_events_block([e], NOW)
    assert "day 3 of 5" in block          # 08,09,10 -> day 3 of 5


def test_summary_truncated():
    e = ev(date="10/06/2026", summary="x" * 300)
    block = render_active_events_block([e], NOW)
    assert "…" in block


# --- gateway prefix ----------------------------------------------------------

class _Gw:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def call(self, name, **kw):
        self.calls.append((name, kw))
        return self.payload


def test_prefix_fetches_and_renders():
    gw = _Gw({"events": [ev(id=5, title="Trip", date="08/06/2026", end_date="12/06/2026")]})
    block = active_events_prefix(gw, NOW)
    assert '#5 "Trip"' in block
    name, kw = gw.calls[0]
    assert name == "get_events"
    assert kw["archived"] == "active_only"
    assert kw["end_date"] == "10/06/2026"


def test_prefix_empty_on_gateway_error():
    assert active_events_prefix(_Gw({"error": "boom"}), NOW) == ""


def test_prefix_swallows_exceptions():
    class Boom:
        def call(self, *a, **k):
            raise RuntimeError("down")
    assert active_events_prefix(Boom(), NOW) == ""
