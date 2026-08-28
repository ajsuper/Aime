"""Unit tests for aime.weekday_check — the weekday/date checksum.

These pin the contract the tool gateway relies on: a weekday that agrees with
its date passes through (minus the consumed field), a disagreement is rejected
before the write reaches the backend, and every path that can't meaningfully
check skips rather than inventing a complaint.
"""

import datetime

import pytest

from aime.weekday_check import check_weekday, WeekdayError


def chk(**payload):
    return check_weekday(payload)


# --- agreement: pass through, field consumed ---------------------------------

def test_matching_weekday_passes_and_is_consumed():
    out = chk(date="25/08/2026", weekday="Tuesday", title="x")
    assert out == {"date": "25/08/2026", "title": "x"}


def test_weekday_is_case_insensitive():
    assert chk(date="25/08/2026", weekday="tuesday") == {"date": "25/08/2026"}
    assert chk(date="25/08/2026", weekday="TUESDAY") == {"date": "25/08/2026"}


def test_weekday_tolerates_surrounding_space():
    assert chk(date="25/08/2026", weekday="  Tuesday ") == {"date": "25/08/2026"}


def test_every_weekday_agrees_with_its_own_date():
    # A full Mon–Sun run, so an off-by-one in the name table can't hide.
    start = datetime.date(2026, 8, 24)  # a Monday
    names = ["Monday", "Tuesday", "Wednesday", "Thursday",
             "Friday", "Saturday", "Sunday"]
    for offset, name in enumerate(names):
        d = start + datetime.timedelta(days=offset)
        assert d.strftime("%A") == name  # guard the fixture itself
        chk(date=d.strftime("%d/%m/%Y"), weekday=name)


# --- disagreement: the reported bug ------------------------------------------

def test_the_reported_failure_is_caught():
    # "Next Tuesday the 23rd" when Tuesday was the 25th.
    with pytest.raises(WeekdayError) as exc:
        chk(date="23/08/2026", weekday="Tuesday")
    msg = str(exc.value)
    assert "is a Sunday, not a Tuesday" in msg
    # Both candidate corrections are named, so the model can fix it itself.
    assert "18/08/2026" in msg
    assert "25/08/2026" in msg


def test_nearest_dates_are_on_either_side():
    with pytest.raises(WeekdayError) as exc:
        chk(date="26/08/2026", weekday="Monday")  # Wednesday
    msg = str(exc.value)
    assert "24/08/2026 (2 days earlier)" in msg
    assert "31/08/2026 (5 days later)" in msg


def test_adjacent_day_uses_singular_day():
    with pytest.raises(WeekdayError) as exc:
        chk(date="26/08/2026", weekday="Tuesday")  # Wednesday, off by one
    assert "25/08/2026 (1 day earlier)" in str(exc.value)


def test_error_tells_the_model_to_ask_when_unclear():
    with pytest.raises(WeekdayError) as exc:
        chk(date="23/08/2026", weekday="Tuesday")
    assert "ask them" in str(exc.value)


def test_unknown_weekday_name_is_rejected():
    with pytest.raises(WeekdayError) as exc:
        chk(date="25/08/2026", weekday="Blursday")
    assert "not a weekday" in str(exc.value)


# --- calendar edges ----------------------------------------------------------

def test_leap_day():
    assert datetime.date(2028, 2, 29).strftime("%A") == "Tuesday"
    chk(date="29/02/2028", weekday="Tuesday")
    with pytest.raises(WeekdayError):
        chk(date="29/02/2028", weekday="Wednesday")


def test_year_boundary_wraps_across_new_year():
    # 31/12/2026 is a Thursday; the nearest Fridays straddle the year end.
    with pytest.raises(WeekdayError) as exc:
        chk(date="31/12/2026", weekday="Friday")
    msg = str(exc.value)
    assert "25/12/2026" in msg
    assert "01/01/2027" in msg


# --- skip paths: can't check, so don't complain ------------------------------

def test_absent_weekday_skips():
    # The web UI's own create/edit calls the gateway directly with no weekday.
    out = chk(date="23/08/2026", title="x")
    assert out == {"date": "23/08/2026", "title": "x"}


def test_blank_weekday_skips():
    assert chk(date="23/08/2026", weekday="") == {"date": "23/08/2026"}


def test_absent_date_skips():
    # Nothing to check against; the backend reports the real problem.
    assert chk(weekday="Tuesday", title="x") == {"title": "x"}


def test_malformed_date_skips_rather_than_shadowing_the_real_error():
    # A weekday complaint here would send the model chasing the wrong problem.
    out = chk(date="not-a-date", weekday="Tuesday")
    assert out == {"date": "not-a-date"}


def test_input_payload_is_not_mutated():
    payload = {"date": "25/08/2026", "weekday": "Tuesday"}
    check_weekday(payload)
    assert payload["weekday"] == "Tuesday"
