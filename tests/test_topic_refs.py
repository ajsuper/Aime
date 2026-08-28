"""Unit tests for aime.topic_refs — `[event-N]` references inside topic prose.

The value of a reference is that it holds no date of its own, so the resolution
must always reflect the *live* event. These tests pin that, plus the two
failure modes: a reference to a deleted event must be reported (not silently
dropped), and the resolution must never end up inside stored content.
"""

import datetime

import pytest

from aime.topic_refs import event_ids, sidecar, strip, TAG_RE, MAX_REFS

TODAY = datetime.date(2026, 8, 26)

EVENTS = {
    42: {"id": 42, "title": "Rachel's party", "date": "22/08/2026",
         "time": "19:00", "status": "unknown"},
    7: {"id": 7, "title": "Dentist", "date": "05/09/2026", "status": "scheduled"},
}


# --- the grammar -------------------------------------------------------------

def test_ids_in_order_deduplicated():
    assert event_ids("[event-42] then [event-7] then [event-42]") == [42, 7]


def test_no_tags_is_empty():
    assert event_ids("just prose about August 22, 2026") == []


@pytest.mark.parametrize("text", ["[event-]", "[event-x]", "event-42", "[graphic-42]"])
def test_near_misses_are_not_tags(text):
    assert event_ids(text) == []


# --- resolution reflects live state -----------------------------------------

def test_resolution_uses_the_live_event_not_the_prose():
    out = sidecar("Party is [event-42]", EVENTS, TODAY)
    assert '"Rachel\'s party"' in out
    assert "Saturday, August 22, 2026 at 19:00" in out


def test_a_moved_event_resolves_to_its_new_date():
    moved = {42: {**EVENTS[42], "date": "05/09/2026", "status": "scheduled"}}
    out = sidecar("Party is [event-42]", moved, TODAY)
    assert "September 5, 2026" in out
    assert "in 10 days" in out


def test_past_is_marked():
    assert "4 days ago (PAST)" in sidecar("[event-42]", EVENTS, TODAY)


def test_today_and_tomorrow():
    evs = {1: {"title": "A", "date": "26/08/2026"}, 2: {"title": "B", "date": "27/08/2026"}}
    out = sidecar("[event-1] [event-2]", evs, TODAY)
    assert "TODAY" in out and "tomorrow" in out


def test_non_default_status_is_surfaced():
    # A canceled event must not read as still happening.
    evs = {1: {"title": "A", "date": "05/09/2026", "status": "canceled"}}
    assert "CANCELED" in sidecar("[event-1]", evs, TODAY)


def test_scheduled_status_is_not_noise():
    assert "SCHEDULED" not in sidecar("[event-7]", EVENTS, TODAY)


def test_archived_is_flagged():
    evs = {1: {"title": "A", "date": "05/09/2026", "archived": True}}
    assert "archived" in sidecar("[event-1]", evs, TODAY)


def test_all_day_event_has_no_time():
    evs = {1: {"title": "A", "date": "05/09/2026"}}
    assert "at " not in sidecar("[event-1]", evs, TODAY).split("[event-1]")[1]


def test_string_keyed_events_also_resolve():
    assert '"Dentist"' in sidecar("[event-7]", {"7": EVENTS[7]}, TODAY)


# --- failure modes -----------------------------------------------------------

def test_a_dangling_reference_is_reported_not_dropped():
    # Silence would read as "no reference here"; the truth is the event is gone
    # and the topic now points at nothing.
    out = sidecar("Gone [event-99]", EVENTS, TODAY)
    assert "[event-99]" in out
    assert "no such event" in out


def test_event_with_no_date_does_not_crash():
    evs = {1: {"title": "A"}}
    assert "no date" in sidecar("[event-1]", evs, TODAY)


def test_malformed_date_is_shown_as_written():
    evs = {1: {"title": "A", "date": "bogus"}}
    assert "bogus" in sidecar("[event-1]", evs, TODAY)


def test_truncation_is_reported():
    text = " ".join(f"[event-{i}]" for i in range(1, MAX_REFS + 5))
    assert "more references not resolved" in sidecar(text, EVENTS, TODAY)


def test_no_refs_means_no_block():
    assert sidecar("plain prose", EVENTS, TODAY) == ""


# --- contamination guard -----------------------------------------------------

def test_a_copied_back_block_is_stripped():
    contaminated = "Party is [event-42]\n\n" + sidecar("[event-42]", EVENTS, TODAY)
    out = strip(contaminated)
    assert "<events" not in out
    # The tag itself is content and must survive.
    assert "[event-42]" in out


def test_strip_is_idempotent():
    once = strip("x\n\n" + sidecar("[event-42]", EVENTS, TODAY))
    assert strip(once) == once


def test_sidecar_tells_the_model_to_prefer_it_over_nearby_prose():
    assert "prefer them over any date written nearby" in sidecar("[event-42]", EVENTS, TODAY)
