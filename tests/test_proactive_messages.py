"""Inline proactive messages and the idle-gap silent rollover.

Covers the two halves of the "continuous thread" change:

  * A proactive message Aime sends out of band (a scheduler reminder, a
    background-agent notification) is recorded *as an assistant turn* so it shows
    inline and the user's reply has coherent context — both on a live backend
    (``append_assistant_message``) and offline, straight to disk
    (``append_proactive_message_offline``). Both keep the message list API-valid
    (opens on a user turn, never two assistant turns back to back) via a hidden
    trigger turn that replay skips.

  * The controller silently rolls onto a fresh session after an idle gap, with no
    user-visible "New conversation" break (no session_restart / splash).
"""

import json
import os

import pytest

import aime.encryption as _enc
from provider_backend import (
    AnthropicMessagesBackend,
    PROACTIVE_TRIGGER_MARKER,
    append_proactive_message_offline,
)


@pytest.fixture
def dek():
    return _enc.generate_dek()


@pytest.fixture
def backend(tmp_path, monkeypatch, dek):
    # The Anthropic client reads its key from env at construction; no call is made
    # in these tests, so a dummy value is enough to build the backend.
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    b = AnthropicMessagesBackend(
        system_prompt="sys",
        model="model",
        schema_files=[],
        conversations_dir=str(tmp_path),
        dek=dek,
    )
    b.new_session()
    return b


def _decrypt_session(path, dek, session_id):
    with open(path, "rb") as f:
        blob = f.read()
    return json.loads(_enc.decrypt_blob(dek, blob, aad=session_id.encode("utf-8")))


# --- live backend: append_assistant_message -------------------------------

def test_append_into_empty_session_opens_with_hidden_user_turn(backend):
    assert backend.append_assistant_message("Reminder: gig at 5:30!") is True
    msgs = backend.messages_snapshot()
    # Hidden trigger turn first (so history opens on a user turn), then the
    # assistant message carrying the real text.
    assert [m["role"] for m in msgs] == ["user", "assistant"]
    assert msgs[0]["content"][0]["text"].startswith(PROACTIVE_TRIGGER_MARKER)
    assert msgs[1]["content"][0]["text"] == "Reminder: gig at 5:30!"


def test_append_after_assistant_turn_inserts_trigger(backend):
    # Simulate a completed turn ending on an assistant message.
    backend._messages.append({"role": "user", "content": [{"type": "text", "text": "hi"}]})
    backend._messages.append({"role": "assistant", "content": [{"type": "text", "text": "hello"}]})
    backend.append_assistant_message("Don't forget your 6pm.")
    roles = [m["role"] for m in backend.messages_snapshot()]
    # ...assistant, then a synthetic user trigger, then the proactive assistant —
    # never two assistant turns in a row.
    assert roles == ["user", "assistant", "user", "assistant"]


def test_append_after_user_turn_needs_no_trigger(backend):
    backend._messages.append({"role": "user", "content": [{"type": "text", "text": "hi"}]})
    backend.append_assistant_message("On it.")
    roles = [m["role"] for m in backend.messages_snapshot()]
    assert roles == ["user", "assistant"]


def test_append_persists_to_disk(backend, tmp_path, dek):
    backend.append_assistant_message("Recorded note.")
    sid = backend.session_id
    path = os.path.join(str(tmp_path), f"{sid}.json.enc")
    data = _decrypt_session(path, dek, sid)
    texts = [
        b["text"]
        for m in data["messages"] if m["role"] == "assistant"
        for b in m["content"]
    ]
    assert "Recorded note." in texts


def test_append_does_not_trigger_a_turn(backend):
    # Recording what Aime already said out of band must not provoke a model reply.
    backend.append_assistant_message("FYI.")
    assert not backend._turn_trigger.is_set()


def test_append_empty_is_noop(backend):
    assert backend.append_assistant_message("   ") is False
    assert backend.messages_snapshot() == []


# --- offline path: append_proactive_message_offline -----------------------

def test_offline_append_creates_session_when_none(tmp_path, dek):
    assert append_proactive_message_offline(str(tmp_path), dek, "Hello there") is True
    files = [n for n in os.listdir(tmp_path) if n.endswith(".json.enc")]
    assert len(files) == 1
    sid = files[0][: -len(".json.enc")]
    data = _decrypt_session(os.path.join(tmp_path, files[0]), dek, sid)
    assert data["messages"][0]["content"][0]["text"].startswith(PROACTIVE_TRIGGER_MARKER)
    assert data["messages"][-1]["role"] == "assistant"
    assert data["messages"][-1]["content"][0]["text"] == "Hello there"


def test_offline_append_targets_latest_session(tmp_path, dek):
    # Two sessions; the proactive message must land in the newer one.
    older = {"id": "msgs-20200101-000000-aaaaaaaa", "summary": "old",
             "saved_at": "2020-01-01T00:00:00",
             "messages": [{"role": "user", "content": [{"type": "text", "text": "old"}]}]}
    newer = {"id": "msgs-20260101-000000-bbbbbbbb", "summary": "new",
             "saved_at": "2026-01-01T00:00:00",
             "messages": [{"role": "user", "content": [{"type": "text", "text": "new"}]},
                          {"role": "assistant", "content": [{"type": "text", "text": "ok"}]}]}
    for data in (older, newer):
        sid = data["id"]
        blob = _enc.encrypt_blob(dek, json.dumps(data).encode("utf-8"), aad=sid.encode("utf-8"))
        with open(os.path.join(tmp_path, f"{sid}.json.enc"), "wb") as f:
            f.write(blob)

    append_proactive_message_offline(str(tmp_path), dek, "Newer note")

    newer_data = _decrypt_session(
        os.path.join(tmp_path, f"{newer['id']}.json.enc"), dek, newer["id"])
    older_data = _decrypt_session(
        os.path.join(tmp_path, f"{older['id']}.json.enc"), dek, older["id"])
    # Newer gained a trigger + assistant turn; older is untouched.
    assert newer_data["messages"][-1]["content"][0]["text"] == "Newer note"
    assert len(older_data["messages"]) == 1


# --- replay hides the hidden trigger turn ---------------------------------

def test_replay_skips_proactive_trigger_turn():
    from aime.replay import replay_messages
    msgs = [
        {"role": "user", "content": [{"type": "text", "text": PROACTIVE_TRIGGER_MARKER}]},
        {"role": "assistant", "content": [{"type": "text", "text": "Gig at 5:30!"}]},
    ]
    events = list(replay_messages(msgs))
    kinds = [e.kind for e in events]
    # The synthetic user turn never surfaces; only Aime's bubble does.
    assert "user_message_shown" not in kinds
    assert kinds == ["assistant_text"]
    assert events[0].text == "Gig at 5:30!"
