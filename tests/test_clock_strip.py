"""Unit tests for the <clock> block's dated weekday strip.

The strip is what turns "next Tuesday" from arithmetic into a lookup, so what
matters is that every date it prints is correct and unambiguous — a strip with a
wrong or bare number would actively cause the bug it exists to prevent.
"""

import datetime

from provider_backend import _weekday_strip


def strip(iso: str) -> str:
    return _weekday_strip(datetime.date.fromisoformat(iso))


def test_today_is_marked_exactly_once():
    s = strip("2026-08-26")
    assert s.count("(today)") == 1
    assert "Wed 26 (today)" in s


def test_covers_two_full_weeks_from_monday():
    s = strip("2026-08-26")  # a Wednesday
    assert "this week: Mon 24 Aug" in s
    assert s.split("next week:")[0].rstrip().endswith("Sun 30;")
    assert "next week: Mon 31 Aug" in s
    assert s.rstrip().endswith("Sun 6.")


def test_every_printed_date_is_the_weekday_it_claims():
    # The whole point of the strip: a wrong pairing here would teach the model
    # the very error the weekday checksum exists to catch.
    import re
    for iso in ("2026-08-26", "2026-12-30", "2028-02-28"):
        today = datetime.date.fromisoformat(iso)
        s = strip(iso)
        monday = today - datetime.timedelta(days=today.weekday())
        month = None
        for i in range(14):
            d = monday + datetime.timedelta(days=i)
            abbr = d.strftime("%a")
            if month is None or d.month != month:
                assert f"{abbr} {d.day} {d.strftime('%b')}" in s, (iso, d)
            else:
                assert re.search(rf"{abbr} {d.day}\b", s), (iso, d)
            month = d.month


def test_month_is_shown_on_rollover_and_never_left_bare():
    # Crossing into September mid-week must qualify the 1st.
    s = strip("2026-08-26")
    assert "Tue 1 Sep" in s
    # The first entry of each week is always qualified.
    assert "this week: Mon 24 Aug" in s and "next week: Mon 31 Aug" in s


def test_year_boundary():
    s = strip("2026-12-30")
    assert "Wed 30 (today)" in s
    assert "Fri 1 Jan" in s          # rolls into the new year mid-week
    assert "Mon 4 Jan" in s


def test_leap_day_appears():
    s = strip("2028-02-28")          # a Monday; the 29th exists in 2028
    assert "Tue 29" in s
    assert "Wed 1 Mar" in s


def test_monday_and_sunday_anchors():
    # Today at either end of its week must still produce a full 7+7.
    for iso in ("2026-08-24", "2026-08-30"):
        s = strip(iso)
        assert s.count("(today)") == 1
        assert "this week: Mon 24 Aug" in s
        assert "next week: Mon 31 Aug" in s


def test_stays_compact():
    # It rides every user turn *after* the cache breakpoint, so it is uncached
    # per-turn spend. Guard against it quietly growing.
    assert len(strip("2026-08-26")) < 260
