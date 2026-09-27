"""Unit tests for aime.datesidecar — resolving absolute dates in stored prose.

The defining property is what this module *refuses* to do. A resolver that
guesses is worse than none, because a system-authored annotation carries
authority the model trusts over its own reading. So the skip cases below matter
at least as much as the resolve cases: every one of them must produce silence,
never a guess.
"""

import datetime

import pytest

from aime.datesidecar import find_dates, sidecar, strip, MAX_ENTRIES

TODAY = datetime.date(2026, 8, 26)


def dates(text):
    return [(raw, d.isoformat()) for raw, d in find_dates(text)]


# --- resolved: unambiguous by construction -----------------------------------

def test_canonical_persisted_form():
    assert dates("Party on August 22, 2026.") == [("August 22, 2026", "2026-08-22")]


def test_iso():
    assert dates("Due 2026-09-03.") == [("2026-09-03", "2026-09-03")]


def test_day_first_with_worded_month():
    assert dates("Started 3 March 2024.") == [("3 March 2024", "2024-03-03")]


def test_abbreviated_month():
    assert dates("Due Aug 22, 2026.") == [("Aug 22, 2026", "2026-08-22")]


def test_ordinal_suffix():
    assert dates("On August 22nd, 2026.") == [("August 22nd, 2026", "2026-08-22")]


def test_case_insensitive():
    assert dates("on AUGUST 22, 2026") and dates("on august 22, 2026")


def test_comma_optional():
    assert dates("August 22 2026") == [("August 22 2026", "2026-08-22")]


# --- skipped silently: anything that would require a guess -------------------

@pytest.mark.parametrize("text", [
    "meeting 6/4",              # June 4 or April 6?
    "meeting 22/08",            # no year
    "meeting on August 22",     # no year
    "meeting 08-22",            # no year
    "on the 22nd",              # no month, no year
    "in 3 days",                # relative — deliberately not our business
    "next Tuesday",
    "sometime in August",
    "version 2026 of the plan",
])
def test_ambiguous_or_relative_text_is_skipped(text):
    assert find_dates(text) == []
    assert sidecar(text, TODAY) == ""


def test_impossible_dates_are_dropped_not_invented():
    assert find_dates("February 30, 2026") == []
    assert find_dates("2026-13-01") == []
    assert find_dates("2026-02-30") == []


def test_a_skipped_date_produces_no_annotation_at_all():
    # Silence, not a hedge — the freshness stamp still carries the age.
    out = sidecar("Party on 22/08 and 6/4.", TODAY)
    assert out == ""


# --- relative rendering ------------------------------------------------------

def test_past_is_marked_unmistakably():
    out = sidecar("Party on August 22, 2026.", TODAY)
    assert '"August 22, 2026" → 4 days ago (PAST)' in out


def test_future():
    assert '"September 3, 2026" → in 8 days' in sidecar("Due September 3, 2026.", TODAY)


def test_today_tomorrow_yesterday():
    assert "→ TODAY" in sidecar("August 26, 2026", TODAY)
    assert "→ tomorrow" in sidecar("August 27, 2026", TODAY)
    assert "→ yesterday (PAST)" in sidecar("August 25, 2026", TODAY)


def test_the_reported_failure_is_surfaced():
    # "Party in 3 days on August 22nd" read a week later: the relative phrase is
    # untouched (not our job), but the absolute date is flagged as past, which
    # is what stops "heads up, you have that party".
    out = sidecar("Party in 3 days on August 22, 2026", TODAY)
    assert "PAST" in out


# --- ordering, dedup, bounds -------------------------------------------------

def test_dates_appear_in_document_order():
    out = dates("Due September 3, 2026 after August 22, 2026.")
    assert [d for _r, d in out] == ["2026-09-03", "2026-08-22"]


def test_same_date_written_twice_is_listed_once():
    assert len(find_dates("August 22, 2026 and again on 22 August 2026")) == 1


def test_truncation_is_reported_not_silent():
    text = " ".join(f"January {i}, 2027" for i in range(1, MAX_ENTRIES + 6))
    out = sidecar(text, TODAY)
    assert "more dates not listed" in out


def test_sidecar_frames_itself_as_something_to_verify():
    out = sidecar("August 22, 2026", TODAY)
    assert "not a" in out and "source of truth" in out


# --- contamination guard -----------------------------------------------------

def test_a_copied_back_sidecar_is_stripped():
    contaminated = "Party notes\n\n" + sidecar("August 22, 2026", TODAY) + "\n"
    assert "<dates" not in strip(contaminated)
    assert "Party notes" in strip(contaminated)


def test_strip_is_idempotent_and_safe_on_plain_text():
    assert strip("plain") == "plain"
    once = strip("x\n\n" + sidecar("August 22, 2026", TODAY))
    assert strip(once) == once
