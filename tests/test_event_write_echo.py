"""Tests for the event-write echo — the confirmation an event write hands back.

Two layers: the pure formatter, and the controller dispatch that must actually
put that string in front of the model instead of the raw {ok, id}.
"""

import pytest

from provider_backend import BackendEvent
from aime.controller import ConversationController
from aime.tool_formatting import format_event_write_echo


def echo(name="CreateEvent", result=None, **inp):
    return format_event_write_echo(name, inp, result or {"ok": True, "id": 42})


# --- the formatter -----------------------------------------------------------

def test_timed_event_spells_weekday_and_month():
    assert echo(date="22/08/2026", time="19:00", title="Rachel's party") == (
        'Created event #42 "Rachel\'s party" — Saturday, August 22, 2026 at 19:00.'
    )


def test_all_day_event_omits_time():
    assert echo(date="22/08/2026", title="Bank holiday") == (
        'Created event #42 "Bank holiday" — Saturday, August 22, 2026.'
    )


def test_edit_says_updated():
    out = echo(name="EditEvent", date="25/08/2026", title="Dentist", id=7)
    assert out.startswith('Updated event #42 "Dentist"')


def test_multi_day_end_is_spelled_too():
    out = echo(date="22/08/2026", time="19:00", title="Trip",
               end_date="24/08/2026", end_time="11:00")
    assert "Saturday, August 22, 2026 at 19:00 → Monday, August 24, 2026 at 11:00" in out


def test_same_day_end_shows_only_the_time():
    out = echo(date="22/08/2026", time="19:00", title="Gym",
               end_date="22/08/2026", end_time="20:30")
    assert "August 22, 2026 at 19:00 → 20:30." in out


def test_duration_is_echoed_when_no_explicit_end():
    # The gateway resolves duration to an absolute end on a copy, so the input
    # still carries the sugar; echoing it beats showing no length at all.
    out = echo(date="22/08/2026", time="19:00", title="Gym", duration="2h")
    assert "(for 2h)" in out


def test_non_default_status_is_flagged():
    assert echo(date="25/08/2026", title="Dentist", status="canceled").endswith("[canceled]")


def test_scheduled_status_is_not_noise():
    assert not echo(date="25/08/2026", title="X", status="scheduled").endswith("]")


def test_malformed_date_is_shown_not_swallowed():
    assert "not-a-date" in echo(date="not-a-date", title="X")


def test_returns_none_for_other_tools_and_failures():
    assert format_event_write_echo("FilterTopics", {}, {"ok": True}) is None
    assert format_event_write_echo("CreateEvent", {}, {"error": "boom"}) is None
    assert format_event_write_echo("CreateEvent", {}, {"ok": False}) is None
    assert format_event_write_echo("CreateEvent", {}, "raw string") is None


# --- controller dispatch -----------------------------------------------------

class _FakeBackend:
    conversations_dir = None

    def __init__(self):
        self.responses = []

    def submit(self, event: BackendEvent):
        if event.kind == "tool_send_response":
            self.responses.append(event)


class _FakeGateway:
    def __init__(self, result=None):
        self.calls = []
        self._result = result or {"ok": True, "id": 42}

    def execute(self, tool_name, tool_input):
        self.calls.append((tool_name, dict(tool_input or {})))
        return self._result


def _fire(tool_name, tool_input, gateway=None):
    backend = _FakeBackend()
    gateway = gateway or _FakeGateway()
    controller = ConversationController(
        backend=backend,
        tool_gateway=gateway,
        worker_spawner=lambda fn: None,
    )
    controller._handle_tool_use(BackendEvent(
        kind="tool_use", tool_name=tool_name,
        tool_input=tool_input, tool_use_id="t1",
    ))
    return backend, gateway


def test_model_receives_the_echo_not_the_raw_ok_id():
    backend, _ = _fire("CreateEvent", {
        "date": "22/08/2026", "weekday": "saturday",
        "time": "19:00", "title": "Rachel's party",
    })
    sent = backend.responses[0].tool_result
    assert isinstance(sent, str)
    assert "Saturday, August 22, 2026 at 19:00" in sent
    assert "ok" not in sent


def test_failed_write_still_forwards_the_raw_error():
    backend, _ = _fire(
        "CreateEvent", {"date": "22/08/2026", "title": "x"},
        gateway=_FakeGateway({"error": "backend exploded"}),
    )
    assert backend.responses[0].tool_result == {"error": "backend exploded"}
