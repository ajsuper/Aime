"""Gateway-level tests for the date guards in ToolGateway._post.

_post is the one choke point both the agent (`execute`) and view-side (`call`)
paths share, so a guard placed there cannot be bypassed. These tests pin that
property: a bad weekday must be reported to the caller and must never reach the
backend, and the checksum field must never be forwarded once consumed.
"""

import pytest

from aime.tool_gateway import ToolGateway


class _Sent:
    """Records what the gateway would have POSTed, and never calls out."""

    def __init__(self):
        self.bodies = []

    def __call__(self, url, json=None, timeout=None):
        self.bodies.append(dict(json or {}))

        class _Resp:
            ok = True

            @staticmethod
            def json():
                return {"ok": True, "id": 42}
        return _Resp()


@pytest.fixture
def gw(monkeypatch):
    sent = _Sent()
    monkeypatch.setattr("aime.tool_gateway.requests.post", sent)
    gateway = ToolGateway()
    gateway._sent = sent
    return gateway


def test_matching_weekday_is_forwarded_without_the_checksum_field(gw):
    result = gw.execute("CreateEvent", {
        "date": "25/08/2026", "weekday": "tuesday", "title": "x",
    })
    assert result == {"ok": True, "id": 42}
    body = gw._sent.bodies[0]
    assert body["date"] == "25/08/2026"
    # Consumed, not persisted — the date is the single source of truth.
    assert "weekday" not in body


def test_mismatched_weekday_is_rejected_before_the_backend(gw):
    result = gw.execute("CreateEvent", {
        "date": "23/08/2026", "weekday": "tuesday", "title": "x",
    })
    assert "error" in result
    assert "is a Sunday, not a Tuesday" in result["error"]
    assert "25/08/2026" in result["error"]
    assert gw._sent.bodies == []          # nothing left the process


def test_the_guard_also_covers_edits(gw):
    result = gw.execute("EditEvent", {
        "id": 7, "date": "23/08/2026", "weekday": "tuesday", "title": "x",
    })
    assert "error" in result
    assert gw._sent.bodies == []


def test_the_guard_also_covers_the_view_side_call_path(gw):
    # `call` bypasses TOOL_NAME_MAP but not _post.
    result = gw.call("create_event", date="23/08/2026", weekday="tuesday")
    assert "error" in result
    assert gw._sent.bodies == []


def test_view_side_writes_without_a_weekday_still_work(gw):
    # The web UI's own create/edit has a real date picker and asserts nothing.
    result = gw.call("create_event", date="23/08/2026", title="x")
    assert result == {"ok": True, "id": 42}
    assert gw._sent.bodies[0]["date"] == "23/08/2026"


def test_weekday_is_not_checked_on_reads(gw):
    # get_events carries dates but no weekday assertion; it must pass through
    # untouched (and still get its now_date/now_time stamp).
    gw.call("get_events", start_date="23/08/2026")
    body = gw._sent.bodies[0]
    assert body["start_date"] == "23/08/2026"
    assert "now_date" in body and "now_time" in body


def test_weekday_check_runs_before_length_normalization(gw):
    # A payload that is wrong in both ways reports the weekday first: a date
    # whose day-of-week is wrong is suspect whatever its end looks like.
    result = gw.execute("CreateEvent", {
        "date": "23/08/2026", "weekday": "tuesday",
        "time": "10:00", "duration": "2h", "end_time": "11:00",
    })
    assert "not a Tuesday" in result["error"]
