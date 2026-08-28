"""Unit tests for aime.freshness — per-section age stamps on topic content.

The load-bearing properties, in order of how much damage their failure does:

1. The body the model reads is byte-identical to what is stored, so a `find`
   built from a read still matches on write. Breaking this breaks the primary
   edit path.
2. Editing one section does not restamp the others. The unsafe direction is old
   text starting to read as new.
3. Nothing the model can copy out of its own context survives a write.
"""

import datetime

import pytest

from aime import freshness as f

TODAY = datetime.date(2026, 8, 26)

STORED = """# Trip notes

## Social
Party in 3 days on August 22nd

## Health
Went running

<!--aime:updated v1
Trip notes=2026-08-10
Social=2026-08-19
Health=2026-08-25
-->
"""


# --- property 1: the body is never altered -----------------------------------

def test_body_handed_to_the_model_is_byte_identical_to_storage():
    body, _ = f.parse(STORED)
    rendered = f.for_model(STORED, TODAY)
    # Everything above the sidecar must match the stored body exactly, so a
    # find/replace patch built from this read still applies.
    assert rendered.split("<freshness")[0].rstrip() == body.rstrip()


def test_headings_are_untouched_by_rendering():
    rendered = f.for_model(STORED, TODAY)
    assert "## Social\nParty in 3 days" in rendered
    assert "(last updated" not in rendered  # never woven into the heading


def test_content_with_no_block_passes_through_unchanged():
    plain = "## Social\nParty on August 22, 2026\n"
    assert f.for_model(plain, TODAY) == plain.rstrip()


# --- property 2: only what changed gets restamped ----------------------------

def test_unchanged_sections_keep_their_date():
    new = "## Social\nParty in 3 days on August 22nd\n\n## Health\nWent running twice\n"
    out = f.restamp(STORED, new, "2026-08-26")
    stamps = f.parse(out)[1]
    assert stamps["Social"] == "2026-08-19"   # untouched
    assert stamps["Health"] == "2026-08-26"   # edited


def test_a_full_rewrite_that_preserves_text_does_not_fake_freshness():
    # ReplaceTopicContents re-sends the whole body; identical sections must not
    # start reading as newly written.
    body, _ = f.parse(STORED)
    out = f.restamp(STORED, body, "2026-08-26")
    stamps = f.parse(out)[1]
    assert stamps["Social"] == "2026-08-19"
    assert stamps["Health"] == "2026-08-25"


def test_new_section_is_stamped_today():
    new = "## Social\nParty in 3 days on August 22nd\n\n## Money\nSaved up\n"
    stamps = f.parse(f.restamp(STORED, new, "2026-08-26"))[1]
    assert stamps["Money"] == "2026-08-26"


def test_renamed_section_drops_its_old_key_rather_than_guessing():
    new = "## Socialising\nParty in 3 days on August 22nd\n"
    stamps = f.parse(f.restamp(STORED, new, "2026-08-26"))[1]
    assert "Social" not in stamps
    assert stamps["Socialising"] == "2026-08-26"


def test_first_ever_write_stamps_everything():
    new = "## Social\nHello\n"
    assert f.parse(f.restamp("", new, "2026-08-26"))[1] == {"Social": "2026-08-26"}


# --- property 3: no contamination from the model's own context ---------------

def test_a_copied_back_sidecar_is_stripped():
    contaminated = "## Social\nHello\n\n" + f.sidecar(STORED, TODAY) + "\n"
    assert "<freshness" not in f.strip(contaminated)
    assert "## Social\nHello" in f.strip(contaminated)


def test_a_copied_back_stored_block_is_stripped():
    assert "aime:updated" not in f.strip(STORED)


def test_strip_is_idempotent():
    once = f.strip(STORED)
    assert f.strip(once) == once


def test_restamp_ignores_a_stamp_block_the_model_echoed_back():
    # The model re-sends content carrying a stale/forged block; what was
    # actually stored wins.
    forged = ("## Social\nParty in 3 days on August 22nd\n\n"
              "<!--aime:updated v1\nSocial=1999-01-01\n-->\n")
    stamps = f.parse(f.restamp(STORED, forged, "2026-08-26"))[1]
    assert stamps["Social"] == "2026-08-19"


# --- the sidecar itself ------------------------------------------------------

def test_sidecar_reports_elapsed_days_so_the_model_need_not_subtract():
    out = f.sidecar(STORED, TODAY)
    assert '"Social" — last written August 19, 2026 (7 days ago)' in out
    assert '"Health" — last written August 25, 2026 (yesterday)' in out


def test_sidecar_says_today_for_a_same_day_write():
    stored = "## Social\nx\n\n<!--aime:updated v1\nSocial=2026-08-26\n-->\n"
    assert "(today)" in f.sidecar(stored, TODAY)


def test_sidecar_dates_use_the_canonical_persisted_format():
    # Month DD, YYYY — the form the system prompt mandates for persisted dates.
    assert "August 19, 2026" in f.sidecar(STORED, TODAY)


def test_sidecar_tells_the_model_what_to_do_with_it():
    out = f.sidecar(STORED, TODAY)
    assert "in 3 days" in out and "not today's" in out


def test_no_stamps_means_no_sidecar():
    assert f.sidecar("## Social\nx\n", TODAY) == ""


def test_preamble_is_labelled_readably():
    stored = "Some opening text\n\n## Social\nx\n\n<!--aime:updated v1\n~preamble=2026-08-19\n-->\n"
    assert "the opening text — last written August 19, 2026" in f.sidecar(stored, TODAY)


# --- robustness: an annotation must never make a topic unreadable ------------

def test_corrupt_block_lines_are_skipped_not_raised():
    stored = ("## Social\nx\n\n<!--aime:updated v1\ngarbage\nSocial=not-a-date\n"
              "Health=2026-08-25\n-->\n")
    body, stamps = f.parse(stored)
    assert stamps == {"Health": "2026-08-25"}
    assert "## Social\nx" in body


def test_duplicate_headings_stay_distinguishable():
    body = "## Social\nfirst\n\n## Social\nsecond\n"
    keys = [k for k, _s, _e in f.split_sections(body)]
    assert keys == ["Social", "Social~2"]


def test_heading_with_an_equals_sign_does_not_corrupt_the_block():
    new = "## A = B\nx\n"
    out = f.restamp("", new, "2026-08-26")
    assert f.parse(out)[1] == {"A  B": "2026-08-26"}


def test_content_with_no_headings_at_all():
    out = f.restamp("", "just a flat note\n", "2026-08-26")
    assert f.parse(out)[1] == {f.PREAMBLE_KEY: "2026-08-26"}


def test_empty_content_produces_no_block():
    assert "aime:updated" not in f.restamp("", "", "2026-08-26")


def test_round_trip_parse_restamp_is_stable():
    once = f.restamp(STORED, f.parse(STORED)[0], "2026-08-26")
    twice = f.restamp(once, f.parse(once)[0], "2026-08-27")
    assert f.parse(once)[1] == f.parse(twice)[1]
