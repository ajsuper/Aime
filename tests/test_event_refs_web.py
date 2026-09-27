"""Server-side pieces behind `[event-N]` references in the web UI.

The security-relevant one is `_foreign_event_tag`. A graphic id is namespaced by
topic handle, so it carries its own authorization; `[event-42]` is a bare
integer. In a shared topic that makes it ambiguous at best and a leak at worst,
so a reference must name an event on the *topic owner's* calendar — the calendar
it will actually resolve against.
"""

import types

import pytest

import frontends.web_app as web_app
from aime.services import CalendarService

EVENTS = [
    {"id": 42, "title": "Rachel's party", "date": "22/08/2026", "status": "scheduled"},
    {"id": 7, "title": "Dentist", "date": "05/09/2026", "status": "canceled"},
]


class _FakeGateway:
    def __init__(self, events=None, boom=False):
        self.calls = []
        self._events = EVENTS if events is None else events
        self._boom = boom

    def call(self, name, **payload):
        self.calls.append((name, payload))
        if self._boom:
            raise RuntimeError("calendar down")
        return list(self._events)


# --- CalendarService.event_by_id --------------------------------------------

def test_event_by_id_finds_the_event():
    svc = CalendarService(_FakeGateway())
    assert svc.event_by_id(42)["title"] == "Rachel's party"


def test_event_by_id_matches_across_string_and_int():
    svc = CalendarService(_FakeGateway())
    assert svc.event_by_id("42")["id"] == 42


def test_event_by_id_returns_none_for_an_unknown_id():
    assert CalendarService(_FakeGateway()).event_by_id(999) is None


def test_event_by_id_includes_archived():
    # A topic can reference an archived event; "archived" is a more useful card
    # than "couldn't load".
    gw = _FakeGateway()
    CalendarService(gw).event_by_id(42)
    assert gw.calls[0][1].get("archived") == "all"


# --- the save-time guard -----------------------------------------------------

@pytest.fixture
def owner_ctx(monkeypatch):
    def _install(gateway):
        ctx = types.SimpleNamespace(calendar_service=CalendarService(gateway))
        monkeypatch.setattr(web_app, "_context_for", lambda uid: ctx)
        return ctx
    return _install


def test_content_with_no_references_passes(owner_ctx):
    owner_ctx(_FakeGateway())
    assert web_app._foreign_event_tag("just prose", 1) is None


def test_a_reference_the_owner_has_passes(owner_ctx):
    owner_ctx(_FakeGateway())
    assert web_app._foreign_event_tag("party is [event-42]", 1) is None


def test_a_reference_the_owner_does_not_have_is_rejected(owner_ctx):
    # The leak case: a recipient adding one of *their* ids to a shared topic,
    # which would then resolve against the owner's calendar.
    owner_ctx(_FakeGateway())
    assert web_app._foreign_event_tag("sneaky [event-999]", 1) == "event-999"


def test_the_first_bad_reference_is_reported(owner_ctx):
    owner_ctx(_FakeGateway())
    assert web_app._foreign_event_tag("[event-42] [event-98] [event-99]", 1) == "event-98"


def test_an_unreadable_calendar_allows_the_save(owner_ctx):
    # Better to let the work through and render "not found" than to block a
    # user's save over a check we couldn't run.
    owner_ctx(_FakeGateway(boom=True))
    assert web_app._foreign_event_tag("party is [event-42]", 1) is None


def test_archived_events_still_count_as_the_owners(owner_ctx):
    owner_ctx(_FakeGateway([{"id": 42, "title": "x", "archived": True}]))
    assert web_app._foreign_event_tag("[event-42]", 1) is None
