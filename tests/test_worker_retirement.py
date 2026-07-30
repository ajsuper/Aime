"""A retired stream worker must exit on its own, not linger until the next message.

Retirement bumps the epoch and pulses the turn trigger — but the session swap
that follows clears that trigger microseconds later, so the outgoing worker
almost never observes its own wake-up. It used to park indefinitely, leaking one
thread per session swap (idle rollover, day roll, /reset, conversation switch);
a production thread dump showed three of them stacked up on a quiet instance.
"""

import threading
import time

import pytest

import aime.encryption as _enc
from provider_backend import AnthropicMessagesBackend


@pytest.fixture
def backend(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    b = AnthropicMessagesBackend(
        system_prompt="sys", model="model", schema_files=[],
        conversations_dir=str(tmp_path), dek=_enc.generate_dek(),
    )
    b.new_session()
    monkeypatch.setattr(type(b), "STALE_WORKER_POLL_SECONDS", 0.05)
    return b


def _drain(backend, kinds, done):
    for event in backend.stream():
        kinds.append(event.kind)
        if event.kind == "session_terminated":
            break
    done.set()


def test_retired_worker_exits_without_a_new_message(backend):
    kinds, done = [], threading.Event()
    threading.Thread(target=_drain, args=(backend, kinds, done), daemon=True).start()
    time.sleep(0.1)          # let it park on the trigger

    # Exactly the production retirement sequence: pulse, then immediately swap
    # (which clears the trigger the worker was supposed to notice).
    backend._terminate_active_stream()
    backend.new_session()

    assert done.wait(5), "retired worker never noticed it had been replaced"
    assert kinds == ["session_terminated"]


def test_live_worker_keeps_waiting(backend):
    kinds, done = [], threading.Event()
    threading.Thread(target=_drain, args=(backend, kinds, done), daemon=True).start()

    # No retirement — the poll must not mistake a quiet session for a dead one.
    time.sleep(0.4)
    assert not done.is_set()
    assert kinds == []
