"""Temporary Chat (incognito): a throwaway thread whose transcript is never
persisted and never folds into the main continuous thread, while tool actions
still persist. Covers the backend persistence suspend and the controller's
enter/exit + proactive-routing behavior.
"""

import os

import pytest

import aime.encryption as _enc
import aime.controller as controller_mod
from provider_backend import (
    AnthropicMessagesBackend,
    BackendEvent,
    SessionInfo,
)
from aime.controller import ConversationController


# --- backend: persistence is suspended for a temporary session ------------

@pytest.fixture
def backend(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    b = AnthropicMessagesBackend(
        system_prompt="sys", model="model", schema_files=[],
        conversations_dir=str(tmp_path), dek=_enc.generate_dek(),
    )
    return b


def _conv_files(tmp_path):
    return [n for n in os.listdir(tmp_path) if n.endswith(".json.enc")]


def test_normal_session_persists(backend, tmp_path):
    backend.new_session()
    backend.submit(BackendEvent(kind="user_send_message", text="hello"))
    assert _conv_files(tmp_path)            # a file was written


def test_ephemeral_session_does_not_persist(backend, tmp_path):
    backend.start_ephemeral_session()
    backend.submit(BackendEvent(kind="user_send_message", text="secret"))
    backend.append_assistant_message("nothing saved")
    assert _conv_files(tmp_path) == []      # nothing hit disk


def test_persistence_resumes_after_ephemeral(backend, tmp_path):
    # A temp session writes nothing; a following real session writes again.
    backend.start_ephemeral_session()
    backend.submit(BackendEvent(kind="user_send_message", text="secret"))
    assert _conv_files(tmp_path) == []
    backend.new_session()
    backend.submit(BackendEvent(kind="user_send_message", text="real"))
    assert _conv_files(tmp_path)


def test_ephemeral_flag_never_enables_persistence_on_incapable_backend(
    tmp_path, monkeypatch
):
    # An agent-style backend (persist_enabled=False) must never write, even via
    # the normal new_session reset of the suspend flag.
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    b = AnthropicMessagesBackend(
        system_prompt="s", model="m", schema_files=[],
        conversations_dir=str(tmp_path), dek=_enc.generate_dek(),
        persist_enabled=False,
    )
    b.new_session()
    b.submit(BackendEvent(kind="user_send_message", text="x"))
    assert _conv_files(tmp_path) == []


# --- controller: enter / exit / routing -----------------------------------

class _FakeBackend:
    conversations_dir = None

    def __init__(self):
        self.submitted = []
        self.reset_calls = 0
        self.appended = []
        self._messages = [{"role": "user", "content": []}]
        self.session_id = "msgs-main-0001"
        self.ephemeral_started = 0
        self.loaded = []

    def submit(self, e):
        self.submitted.append(e)

    def reset(self):
        self.reset_calls += 1
        self._messages = []

    def messages_snapshot(self):
        return list(self._messages)

    def append_assistant_message(self, text, pid=""):
        self._messages.append({"role": "assistant",
                               "content": [{"type": "text", "text": text}],
                               "pid": pid})
        self.appended.append(text)
        return True

    def start_ephemeral_session(self):
        self.ephemeral_started += 1
        self._messages = []
        self.session_id = "temp-xxxx"
        return self.session_id

    def load_session(self, sid):
        self.loaded.append(sid)
        self.session_id = sid
        self._messages = [{"role": "user", "content": []}]

    def list_sessions(self):
        return [SessionInfo(id="msgs-main-0001", summary="main",
                            saved_at="2026-01-01T00:00:00")]

    def interrupt_turn(self):
        pass


def _controller():
    backend = _FakeBackend()
    events = []
    c = ConversationController(
        backend=backend, tool_gateway=object(), worker_spawner=lambda fn: None,
    )
    c._user_first_interaction = False
    c._maybe_start_onboarding = lambda: None
    c.subscribe(events.append)
    return c, backend, events


def _severities(events):
    return [e.severity for e in events if e.kind == "notice"]


def test_enter_temporary_starts_ephemeral_and_signals():
    c, backend, events = _controller()
    events.clear()
    c.enter_temporary_chat()
    assert c.is_temporary is True
    assert backend.ephemeral_started == 1
    assert c._main_session_id == "msgs-main-0001"   # remembered for exit
    assert "session_restart" in [e.kind for e in events]   # view cleared
    assert "temporary" in _severities(events)              # banner armed


def test_temporary_is_idempotent():
    c, backend, events = _controller()
    c.enter_temporary_chat()
    c.enter_temporary_chat()
    assert backend.ephemeral_started == 1


def test_exit_temporary_restores_main_and_signals():
    c, backend, events = _controller()
    c.enter_temporary_chat()
    events.clear()
    c.exit_temporary_chat()
    assert c.is_temporary is False
    assert backend.loaded == ["msgs-main-0001"]     # main thread reloaded
    assert "temporary_end" in _severities(events)   # banner dropped


def test_proactive_routes_to_main_while_temporary():
    c, backend, events = _controller()
    c.enter_temporary_chat()
    # In temp mode the live session declines the inline write so the caller
    # (web_app) routes it to the main thread file instead.
    assert c.deliver_inline_proactive("Reminder fired") is False
    assert backend.appended == []


def test_proactive_records_inline_when_not_temporary():
    c, backend, events = _controller()
    assert c.deliver_inline_proactive("Heads up") is True
    assert backend.appended == ["Heads up"]


def test_resume_emits_session_divider_with_cursor():
    # Restoring the main thread (here via exit) labels it with a divider carrying
    # the session id — the scroll-back cursor the frontend pages /history from.
    c, backend, events = _controller()
    c.enter_temporary_chat()
    events.clear()
    c.exit_temporary_chat()
    dividers = [e for e in events if e.kind == "session_divider"]
    assert dividers
    assert dividers[0].payload["session_id"] == "msgs-main-0001"
    assert dividers[0].payload["title"] == "main"


def test_no_idle_rollover_in_temporary(monkeypatch):
    import time
    monkeypatch.setattr(controller_mod, "IDLE_ROLLOVER_SECONDS", 3600)
    c, backend, events = _controller()
    c.enter_temporary_chat()
    c._last_activity = time.time() - 7200
    backend.reset_calls = 0
    c.dispatch_input("still here?")
    assert backend.reset_calls == 0     # temp chats never roll over
