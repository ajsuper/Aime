"""Scroll-back history read model: reading stored sessions, rendering them like
the live transcript, and paginating older sessions for the /history endpoint.
"""

import json
import os

import aime.encryption as _enc
from provider_backend import (
    PROACTIVE_TRIGGER_MARKER,
    SessionInfo,
    read_session_messages,
)
import frontends.web_app as web_app


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
    assert s1["events"][0]["kind"] == "user_message_shown"
