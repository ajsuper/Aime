"""Scroll-back history read model: reading stored sessions, rendering them like
the live transcript, and paginating older sessions for the /history endpoint.
"""

import json
import os

import aime.encryption as _enc
import datetime

from provider_backend import (
    PROACTIVE_TRIGGER_MARKER,
    SessionInfo,
    read_session_messages,
    session_started_at,
)
import frontends.web_app as web_app


# --- session_started_at (absolute instant from the id's local stamp) -------

def test_session_started_at_returns_aware_instant():
    iso = session_started_at("msgs-20260628-143000-abcd1234")
    assert iso  # non-empty
    dt = datetime.datetime.fromisoformat(iso)
    assert dt.tzinfo is not None          # tz-aware → renderable in any zone
    assert (dt.year, dt.month, dt.day) == (2026, 6, 28)
    assert (dt.hour, dt.minute) == (14, 30)


def test_session_started_at_bad_id_is_empty():
    assert session_started_at("not-a-session") == ""
    assert session_started_at("") == ""


# --- daily grouping --------------------------------------------------------

def test_group_days_buckets_newest_first():
    infos = [
        SessionInfo(id="s5", summary="", saved_at="2026-06-28T15:00:00"),
        SessionInfo(id="s4", summary="", saved_at="2026-06-28T09:00:00"),
        SessionInfo(id="s3", summary="", saved_at="2026-06-27T20:00:00"),
        SessionInfo(id="s2", summary="", saved_at="2026-06-27T08:00:00"),
        SessionInfo(id="s1", summary="", saved_at="2026-06-25T12:00:00"),
    ]
    day_of = {"s5": "2026-06-28", "s4": "2026-06-28", "s3": "2026-06-27",
              "s2": "2026-06-27", "s1": "2026-06-25"}
    days = web_app._group_days(infos, lambda sid: day_of[sid])
    assert [d["date"] for d in days] == ["2026-06-28", "2026-06-27", "2026-06-25"]
    assert [d["count"] for d in days] == [2, 2, 1]
    # last_activity is the newest session of the day (first seen).
    assert days[0]["last_activity"] == "2026-06-28T15:00:00"


def test_group_days_skips_unparseable():
    infos = [SessionInfo(id="good", summary="", saved_at="x"),
             SessionInfo(id="bad", summary="", saved_at="y")]
    days = web_app._group_days(
        infos, lambda sid: "2026-06-28" if sid == "good" else "")
    assert [d["date"] for d in days] == ["2026-06-28"]


def test_session_local_day_uses_timezone():
    # A late-evening UTC instant lands on the next day in a far-east zone and the
    # previous day in a far-west zone — grouping must honor the user's tz.
    sid = "msgs-20260628-233000-aa"   # 23:30 server-local
    # Whatever the server tz, the helper returns *a* valid YYYY-MM-DD.
    day = web_app._session_local_day(sid, "")
    assert len(day) == 10 and day[4] == "-"


def _write_session(tmp_path, dek, session_id, data):
    blob = _enc.encrypt_blob(
        dek, json.dumps(data).encode("utf-8"), aad=session_id.encode("utf-8"))
    with open(os.path.join(tmp_path, f"{session_id}.json.enc"), "wb") as f:
        f.write(blob)


# --- read_session_messages -------------------------------------------------

def test_read_session_messages_roundtrip(tmp_path):
    dek = _enc.generate_dek()
    msgs = [{"role": "user", "content": [{"type": "text", "text": "hi"}]}]
    _write_session(tmp_path, dek, "msgs-1", {"id": "msgs-1", "messages": msgs})
    assert read_session_messages(str(tmp_path), dek, "msgs-1") == msgs


def test_read_session_messages_missing_returns_none(tmp_path):
    assert read_session_messages(str(tmp_path), _enc.generate_dek(), "nope") is None


# --- _session_render_events ------------------------------------------------

def test_render_events_strips_prefix_and_renders_html():
    msgs = [
        {"role": "user", "content": [{"type": "text",
            "text": "<active_events>\n- Gig\n</active_events>\nwhat's today?"}]},
        {"role": "assistant", "content": [{"type": "text", "text": "Your gig is at 5."}]},
    ]
    events = web_app._session_render_events(msgs)
    kinds = [e["kind"] for e in events]
    assert kinds == ["user_message_shown", "assistant_html"]
    # Hidden prefix stripped from the user bubble.
    assert events[0]["text"] == "what's today?"
    # Assistant text comes back as rendered HTML, not raw.
    assert "Your gig is at 5." in events[1]["text"]


def test_render_events_skips_proactive_trigger():
    msgs = [
        {"role": "user", "content": [{"type": "text", "text": PROACTIVE_TRIGGER_MARKER}]},
        {"role": "assistant", "content": [{"type": "text", "text": "Reminder!"}]},
    ]
    events = web_app._session_render_events(msgs)
    assert [e["kind"] for e in events] == ["assistant_html"]


# --- _build_history_page (pagination) --------------------------------------

def _infos(*ids):
    # Newest-first, like list_sessions(); saved_at descending.
    return [SessionInfo(id=i, summary=f"title-{i}", saved_at=f"2026-01-{n:02d}T00:00:00")
            for n, i in zip(range(len(ids), 0, -1), ids)]


def _loader(_id):
    return [{"role": "user", "content": [{"type": "text", "text": "hello"}]}]


def test_history_page_first_page_excludes_cursorless_newest():
    # before="" → newest page; with limit 2 of 4 sessions, has_more True.
    infos = _infos("s4", "s3", "s2", "s1")   # s4 newest … s1 oldest
    page = web_app._build_history_page(infos, "", 2, _loader)
    assert [s["id"] for s in page["sessions"]] == ["s3", "s4"]   # oldest-first
    assert page["has_more"] is True


def test_history_page_after_cursor_returns_older():
    infos = _infos("s4", "s3", "s2", "s1")
    # Oldest currently shown is s4 (the resumed session); page older than it.
    page = web_app._build_history_page(infos, "s4", 2, _loader)
    assert [s["id"] for s in page["sessions"]] == ["s2", "s3"]   # oldest-first
    assert page["has_more"] is True


def test_history_page_last_page_sets_has_more_false():
    infos = _infos("s4", "s3", "s2", "s1")
    page = web_app._build_history_page(infos, "s2", 5, _loader)
    assert [s["id"] for s in page["sessions"]] == ["s1"]
    assert page["has_more"] is False


def test_history_page_unknown_cursor_is_end():
    infos = _infos("s2", "s1")
    page = web_app._build_history_page(infos, "deleted-id", 5, _loader)
    assert page == {"sessions": [], "has_more": False}


def test_history_page_carries_title_and_events():
    infos = _infos("s2", "s1")
    page = web_app._build_history_page(infos, "s2", 5, _loader)
    s1 = page["sessions"][0]
    assert s1["id"] == "s1"
    assert s1["title"] == "title-s1"
    assert "started_at" in s1          # absolute instant for tz-correct rendering
    assert s1["events"][0]["kind"] == "user_message_shown"


def test_history_page_derives_started_at_from_real_id():
    infos = [SessionInfo(id="msgs-20260628-143000-aa", summary="x",
                         saved_at="2026-06-28T14:35:00")]
    page = web_app._build_history_page(infos, "", 5, _loader)
    assert page["sessions"][0]["started_at"]   # non-empty, tz-aware instant


# --- in-memory replay backlog (bounded /stream replay + /backlog paging) -----

def _hist(*kinds):
    """Build a fake in-memory replay cache: one event per kind, `_seq` counting
    from 1 in order (mirrors how _broadcast stamps them)."""
    return [{"kind": k, "_seq": i + 1} for i, k in enumerate(kinds)]


def test_normalize_backlog_keeps_only_self_contained_render_kinds():
    events = _hist(
        "session_divider", "user_message_shown", "assistant_text",
        "assistant_html", "graphic", "proactive_message", "tool_call",
        "notice", "error", "turn_routing", "assistant_thinking",
    )
    kept = [e["kind"] for e in web_app._normalize_backlog_events(events)]
    assert kept == [
        "session_divider", "user_message_shown", "assistant_html",
        "graphic", "proactive_message", "tool_call",
    ]


def test_backlog_slice_returns_events_older_than_cursor_oldest_first():
    # seqs 1..6, all renderable.
    hist = _hist("user_message_shown", "assistant_html",
                 "user_message_shown", "assistant_html",
                 "user_message_shown", "assistant_html")
    # Cursor at the 5th event → everything with _seq < 5, newest 2 of them.
    page = web_app._backlog_slice(hist, before_seq=5, limit=2)
    assert [e["_seq"] for e in page["events"]] == [3, 4]  # oldest-first
    assert page["has_more"] is True                       # seqs 1,2 remain


def test_backlog_slice_last_page_sets_has_more_false():
    hist = _hist("user_message_shown", "assistant_html", "user_message_shown")
    page = web_app._backlog_slice(hist, before_seq=3, limit=10)
    assert [e["_seq"] for e in page["events"]] == [1, 2]
    assert page["has_more"] is False


def test_backlog_slice_excludes_the_cursor_event_itself():
    hist = _hist("user_message_shown", "assistant_html")
    page = web_app._backlog_slice(hist, before_seq=1, limit=10)
    assert page["events"] == []            # nothing strictly older than seq 1
    assert page["has_more"] is False


def test_backlog_slice_counts_has_more_after_kind_filtering():
    # Non-renderable events below the cursor must not inflate has_more.
    hist = [
        {"kind": "notice", "_seq": 1},
        {"kind": "error", "_seq": 2},
        {"kind": "user_message_shown", "_seq": 3},
        {"kind": "assistant_html", "_seq": 4},
        {"kind": "user_message_shown", "_seq": 5},
    ]
    page = web_app._backlog_slice(hist, before_seq=5, limit=2)
    # Only 2 renderable events are older than seq 5 → they all fit, no more.
    assert [e["_seq"] for e in page["events"]] == [3, 4]
    assert page["has_more"] is False
