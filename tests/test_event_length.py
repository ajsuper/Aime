"""Unit tests for aime.event_length — the duration→absolute-end normalizer.

These pin the contract the tool gateway relies on: a `duration` sugar resolves
to a concrete `end_date`/`end_time`, an explicit end is validated and passed
through, illegal combinations are rejected before they reach the backend, and a
point-in-time event is left untouched.
"""

import pytest

from aime.event_length import normalize_event_length, EventLengthError


def norm(**payload):
    return normalize_event_length(payload)


# --- point events: nothing to do --------------------------------------------

def test_point_event_passes_through():
    out = norm(date="10/06/2026", time="09:00", title="x")
    assert "end_date" not in out and "end_time" not in out
    assert out["title"] == "x"


def test_dateless_payload_without_length_is_untouched():
    # Some edits omit date entirely; with no length that's fine.
    out = norm(title="x")
    assert out == {"title": "x"}


# --- duration: timed events --------------------------------------------------

def test_minutes_string():
    out = norm(date="10/06/2026", time="10:00", duration="90m")
    assert out["end_date"] == "10/06/2026"
    assert out["end_time"] == "11:30"
    assert "duration" not in out


def test_bare_number_is_minutes():
    out = norm(date="10/06/2026", time="10:00", duration=45)
    assert out["end_time"] == "10:45"


def test_hours_and_minutes_combo():
    out = norm(date="10/06/2026", time="10:00", duration="1h30m")
    assert out["end_time"] == "11:30"


def test_duration_rolls_past_midnight():
    out = norm(date="10/06/2026", time="23:30", duration="2h")
    assert out["end_date"] == "11/06/2026"
    assert out["end_time"] == "01:30"


def test_sub_day_duration_without_time_rejected():
    with pytest.raises(EventLengthError):
        norm(date="10/06/2026", duration="2h")


# --- duration: spans on all-day events --------------------------------------

def test_days_on_all_day_event():
    out = norm(date="10/06/2026", duration="3d")
    assert out["end_date"] == "13/06/2026"
    assert out["end_time"] == ""


def test_months_are_calendar_aware():
    out = norm(date="31/01/2026", duration="1mo")
    # relativedelta clamps to the month end (Feb has no 31st).
    assert out["end_date"] == "28/02/2026"


def test_two_month_trip():
    out = norm(date="10/06/2026", duration="2mo")
    assert out["end_date"] == "10/08/2026"
    assert out["end_time"] == ""


def test_weeks():
    out = norm(date="10/06/2026", duration="2w")
    assert out["end_date"] == "24/06/2026"


# --- explicit end ------------------------------------------------------------

def test_explicit_end_time_same_day():
    out = norm(date="10/06/2026", time="10:00", end_time="11:00")
    assert out["end_date"] == "10/06/2026"
    assert out["end_time"] == "11:00"


def test_explicit_end_date_multi_day():
    out = norm(date="10/06/2026", end_date="14/06/2026")
    assert out["end_date"] == "14/06/2026"
    assert out["end_time"] == ""


def test_end_before_start_rejected():
    with pytest.raises(EventLengthError):
        norm(date="10/06/2026", time="10:00", end_time="09:00")


def test_end_date_before_start_rejected():
    with pytest.raises(EventLengthError):
        norm(date="10/06/2026", end_date="09/06/2026")


def test_end_time_without_start_time_rejected():
    with pytest.raises(EventLengthError):
        norm(date="10/06/2026", end_time="11:00")


# --- mutual exclusion & junk -------------------------------------------------

def test_duration_and_end_together_rejected():
    with pytest.raises(EventLengthError):
        norm(date="10/06/2026", time="10:00", duration="1h", end_time="12:00")


def test_garbage_duration_rejected():
    with pytest.raises(EventLengthError):
        norm(date="10/06/2026", time="10:00", duration="soon")


def test_empty_duration_treated_as_no_length():
    out = norm(date="10/06/2026", time="10:00", duration="")
    assert "end_date" not in out


def test_clearing_end_with_empty_end_date():
    # An explicit empty end_date with no start time / no other end → no length.
    out = norm(date="10/06/2026", end_date="")
    assert out.get("end_date", "") == ""


# --- gateway integration: normalization runs on the shared choke point -------

class _FakeResp:
    ok = True
    text = ""

    def json(self):
        return {"ok": True}


def _gateway_with_capture(monkeypatch):
    """A ToolGateway whose HTTP POST is captured instead of sent, returning the
    list that receives each forwarded body."""
    import aime.tool_gateway as tg

    sent = []
    monkeypatch.setattr(
        tg.requests, "post",
        lambda url, json=None, timeout=None: sent.append(json) or _FakeResp(),
    )
    return tg.ToolGateway(user_id=7), sent


def test_gateway_execute_normalizes_duration(monkeypatch):
    gw, sent = _gateway_with_capture(monkeypatch)
    gw.execute("CreateEvent", {"date": "10/06/2026", "time": "10:00", "duration": "2h"})
    assert sent[-1]["end_time"] == "12:00"
    assert "duration" not in sent[-1]
    assert sent[-1]["user_id"] == 7


def test_gateway_call_path_also_normalizes(monkeypatch):
    gw, sent = _gateway_with_capture(monkeypatch)
    gw.call("replace_event", id=3, date="10/06/2026", time="10:00", duration="30m")
    assert sent[-1]["end_time"] == "10:30"


def test_gateway_rejects_bad_combo_without_posting(monkeypatch):
    gw, sent = _gateway_with_capture(monkeypatch)
    r = gw.call("replace_event", id=3, date="10/06/2026", time="10:00",
                duration="1h", end_time="12:00")
    assert "error" in r
    assert sent == []   # never forwarded to the backend


def test_gateway_leaves_reads_untouched(monkeypatch):
    gw, sent = _gateway_with_capture(monkeypatch)
    gw.call("get_events", filter_by_date=True, end_date="40/06/2026")
    # end_date here is a get_events *filter* bound, not an event end — must pass
    # through verbatim (no normalization on read tools).
    assert sent[-1]["end_date"] == "40/06/2026"
