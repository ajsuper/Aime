"""Interrupt cleanup must never park the worker while holding the backend lock.

An interrupt can race a session swap (reset, idle/day rollover, conversation
switch): `stop_model()` fires the interrupt, the swap empties `_messages`, and
the in-flight worker only then reaches `_cleanup_interrupted_turn`. That path
used to call `_persist()` while holding `self._lock` — a plain, non-reentrant
Lock — which deadlocked the worker *with the lock held*, wedging every turn,
/send and /stream for that user until the process restarted.
"""

import threading

import pytest

import aime.encryption as _enc
from provider_backend import AnthropicMessagesBackend


@pytest.fixture
def backend(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    return AnthropicMessagesBackend(
        system_prompt="sys", model="model", schema_files=[],
        conversations_dir=str(tmp_path), dek=_enc.generate_dek(),
    )


def _run_with_timeout(fn, timeout=5.0):
    """Run `fn` on a worker thread; True if it returned within `timeout`."""
    done = threading.Event()

    def target():
        fn()
        done.set()

    threading.Thread(target=target, daemon=True).start()
    return done.wait(timeout)


def test_interrupt_cleanup_on_swapped_out_session_returns(backend, tmp_path):
    # The state left by a swap that raced the interrupt: fresh session id,
    # empty history, no assistant placeholder to reconcile.
    backend.new_session()

    assert _run_with_timeout(lambda: backend._cleanup_interrupted_turn(None)), \
        "cleanup deadlocked on an emptied session"

    # And the lock is genuinely free afterwards — a wedged worker would hold it
    # forever, so every later reader (submit, history_fingerprint, stream) hangs.
    assert _run_with_timeout(backend.messages_snapshot), \
        "backend lock still held after cleanup"

    # Nothing to save: an empty session stays off disk (see new_session).
    assert [n for n in tmp_path.iterdir() if n.name.endswith(".json.enc")] == []


def test_interrupt_cleanup_still_repairs_a_real_history(backend, tmp_path):
    backend.new_session()
    with backend._lock:
        backend._messages.append(
            {"role": "user", "content": [{"type": "text", "text": "hi"}]}
        )

    assert _run_with_timeout(lambda: backend._cleanup_interrupted_turn(None))

    msgs = backend.messages_snapshot()
    # A trailing user message gets the synthetic assistant stub so the next
    # user turn still has a valid predecessor.
    assert [m["role"] for m in msgs] == ["user", "assistant"]
    assert msgs[-1]["content"][0]["text"] == "[interrupted]"
    # This one *is* worth persisting.
    assert [n for n in tmp_path.iterdir() if n.name.endswith(".json.enc")]
