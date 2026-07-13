"""Durable-authority sync verification for the chat replay stream.

Covers the pieces that keep the SSE replay cache honest against the durable
transcript, so a refresh actually recovers missing messages and a sent message
never silently vanishes:

  * ``message_identity`` / ``AnthropicMessagesBackend.history_fingerprint`` — a
    cheap, content-addressed (count, digest) of the message list. Deterministic
    across backends (so a rebuild yields the same fingerprint as the live one)
    and sensitive to an out-of-band rewrite like compaction.

  * ``ConversationController.resync_view`` — re-emits the current session's
    transcript from the durable store (session_restart + divider + replayed
    messages), and no-ops mid-turn and in a Temporary Chat.

  * ``client_msg_id`` round-trip — the id a /send carries rides back on the
    ``user_message_shown`` event so the frontend retires the right optimistic
    bubble, on both the immediate and the queued (busy → turn_end drain) paths.
"""

import threading

import pytest

import aime.encryption as _enc
from provider_backend import (
    AnthropicMessagesBackend,
    BackendEvent,
    SessionInfo,
    message_identity,
)
from aime.controller import ConversationController
from frontends.web_app import UserContext


# --- fingerprint (real backend) -------------------------------------------

@pytest.fixture
def dek():
    return _enc.generate_dek()


@pytest.fixture
def backend(tmp_path, monkeypatch, dek):
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


def _seed(backend, msgs):
    """Install a message list directly (bypassing the model) so fingerprint
    behavior can be exercised without a live turn."""
    with backend._lock:
        backend._messages = list(msgs)


def test_message_identity_prefers_pid():
    # A proactive message's stable pid is its identity verbatim, so live and
    # rebuilt chains agree without re-deriving.
    msg = {"role": "assistant", "content": [{"type": "text", "text": "hi"}],
           "pid": "p-abc123"}
    assert message_identity(msg) == "p-abc123"


def test_message_identity_content_addressed_and_deterministic():
    a = {"role": "user", "content": [{"type": "text", "text": "hello"}]}
    b = {"role": "user", "content": [{"type": "text", "text": "hello"}]}
    c = {"role": "user", "content": [{"type": "text", "text": "goodbye"}]}
    # Same role+content → same id (deterministic, no pid needed); different
    # content → different id.
    assert message_identity(a) == message_identity(b)
    assert message_identity(a) != message_identity(c)
    # Role participates too: identical content under a different role differs.
    d = {"role": "assistant", "content": [{"type": "text", "text": "hello"}]}
    assert message_identity(a) != message_identity(d)


def test_history_fingerprint_counts_and_is_stable(backend):
    msgs = [
        {"role": "user", "content": [{"type": "text", "text": "hi"}]},
        {"role": "assistant", "content": [{"type": "text", "text": "hey"}]},
    ]
    _seed(backend, msgs)
    count, digest = backend.history_fingerprint()
    assert count == 2
    # Stable across repeated calls on unchanged state.
    assert backend.history_fingerprint() == (count, digest)


def test_history_fingerprint_cross_backend_determinism(tmp_path, monkeypatch, dek):
    # Two independent backends over the same messages must agree — this is what
    # lets a rebuild-from-durable produce a fingerprint identical to the live
    # one, so reconcile never false-positives.
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    msgs = [
        {"role": "user", "content": [{"type": "text", "text": "one"}]},
        {"role": "assistant", "content": [{"type": "text", "text": "two"}]},
    ]

    def _mk():
        b = AnthropicMessagesBackend(
            system_prompt="sys", model="model", schema_files=[],
            conversations_dir=str(tmp_path), dek=dek,
        )
        b.new_session()
        _seed(b, msgs)
        return b

    assert _mk().history_fingerprint() == _mk().history_fingerprint()


def test_history_fingerprint_changes_on_prefix_rewrite(backend):
    # Compaction rewrites the message prefix (old turns → one summary message).
    # Even if the count barely moves, the digest must change so a reconnect
    # rebuilds instead of replaying a cache that no longer matches.
    original = [
        {"role": "user", "content": [{"type": "text", "text": f"m{i}"}]}
        for i in range(6)
    ]
    _seed(backend, original)
    before = backend.history_fingerprint()
    compacted = [
        {"role": "user", "content": [{"type": "text", "text": "[summary]"}]},
        *original[4:],
    ]
    _seed(backend, compacted)
    after = backend.history_fingerprint()
    assert after != before


# --- controller: resync_view + client_msg_id ------------------------------

class _FakeBackend:
    """Enough of AnthropicMessagesBackend for controller-level tests: records
    submits and exposes a durable message list to replay from."""

    conversations_dir = None
    session_id = "msgs-20260713-101010-abcd1234"

    def __init__(self, messages=None):
        self.submitted = []
        self._messages = list(messages or [])

    def submit(self, event):
        self.submitted.append(event)

    def reset(self):
        self._messages = []

    def messages_snapshot(self):
        return list(self._messages)

    def list_sessions(self):
        return [SessionInfo(id=self.session_id, summary="Chat", saved_at="")]


def _controller(messages=None):
    backend = _FakeBackend(messages)
    events = []
    c = ConversationController(
        backend=backend,
        tool_gateway=object(),
        worker_spawner=lambda fn: None,
    )
    c._user_first_interaction = False
    c.subscribe(events.append)
    return c, backend, events


def _kinds(events):
    return [e.kind for e in events]


def test_resync_view_replays_durable_transcript():
    msgs = [
        {"role": "user", "content": [{"type": "text", "text": "hello"}]},
        {"role": "assistant", "content": [{"type": "text", "text": "hi there"}]},
    ]
    c, _backend, events = _controller(msgs)
    events.clear()
    c.resync_view()
    kinds = _kinds(events)
    # Clears the frontend cache (session_restart), re-lays the divider, then
    # replays the persisted messages.
    assert kinds[0] == "session_restart"
    assert "session_divider" in kinds
    assert "user_message_shown" in kinds
    assert "assistant_text" in kinds


def test_resync_view_noop_mid_turn():
    c, _backend, events = _controller([
        {"role": "user", "content": [{"type": "text", "text": "hi"}]},
    ])
    # Claim the turn (busy): resync must not wipe the view under a live reply.
    c.dispatch_input("hi")
    events.clear()
    c.resync_view()
    assert events == []


def test_resync_view_noop_in_temporary_chat():
    c, _backend, events = _controller([
        {"role": "user", "content": [{"type": "text", "text": "hi"}]},
    ])
    c._temporary = True
    events.clear()
    c.resync_view()
    assert events == []


def test_client_msg_id_echoed_on_user_message_shown():
    c, _backend, events = _controller()
    c.dispatch_input("hello", client_msg_id="c-xyz")
    shown = [e for e in events if e.kind == "user_message_shown"]
    assert len(shown) == 1
    assert shown[0].client_msg_id == "c-xyz"


def test_client_msg_id_survives_queue_and_drain():
    c, backend, events = _controller()
    # First message claims the turn.
    c.dispatch_input("first", client_msg_id="c-1")
    # Second arrives mid-turn → queued with its id preserved.
    c.dispatch_input("second", client_msg_id="c-2")
    queued = [e for e in events if e.kind == "user_message_queued"]
    assert len(queued) == 1
    events.clear()
    # End the turn: the queued message drains and its echo must still carry c-2.
    c._handle_backend_event(BackendEvent(kind="turn_end", stop_reason="end_turn"))
    shown = [e for e in events if e.kind == "user_message_shown"]
    assert len(shown) == 1
    assert shown[0].client_msg_id == "c-2"


# --- integration: the reconcile glue on the real UserContext methods -------
#
# Driving the actual UserContext methods (unbound, on a light stand-in) against
# a real backend + real controller proves the end-to-end centerpiece — a
# refresh reconciles the replay cache against the durable store — without
# standing up the whole Flask/auth/gateway stack UserContext.__init__ needs.


class _CtxStub:
    """The slice of UserContext state the reconcile/broadcast methods touch."""

    def __init__(self, backend, controller):
        self._backend = backend
        self.controller = controller
        self._history_lock = threading.Lock()
        self._subscribers_lock = threading.Lock()
        self._history = []
        self._history_seq = 0
        self._history_source_fp = None
        self._client_queues = []


def _real_stack(tmp_path, monkeypatch, dek, messages):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    backend = AnthropicMessagesBackend(
        system_prompt="sys", model="model", schema_files=[],
        conversations_dir=str(tmp_path), dek=dek,
    )
    backend.new_session()
    _seed(backend, messages)
    events = []
    controller = ConversationController(
        backend=backend, tool_gateway=object(), worker_spawner=lambda fn: None,
    )
    controller._user_first_interaction = False
    controller.subscribe(events.append)
    ctx = _CtxStub(backend, controller)
    # In sync to start: the cache reflects the seeded message list.
    ctx._history_source_fp = backend.history_fingerprint()
    return ctx, backend, events


def test_reconcile_noop_when_in_sync(tmp_path, monkeypatch, dek):
    ctx, _b, events = _real_stack(tmp_path, monkeypatch, dek, [
        {"role": "user", "content": [{"type": "text", "text": "hi"}]},
    ])
    events.clear()
    UserContext.reconcile_history_with_durable(ctx)
    # Fingerprint matches → no rebuild, nothing re-emitted.
    assert events == []


def test_reconcile_rebuilds_on_drift(tmp_path, monkeypatch, dek):
    # The exact bug #2 shape: the durable message list gained a message the
    # replay cache never saw (its fingerprint went stale). A reconnect must
    # rebuild from durable instead of replaying the stale cache forever.
    ctx, backend, events = _real_stack(tmp_path, monkeypatch, dek, [
        {"role": "user", "content": [{"type": "text", "text": "hi"}]},
        {"role": "assistant", "content": [{"type": "text", "text": "hello"}]},
    ])
    # Out-of-band append (simulating an offline proactive / a dropped broadcast):
    # the message list grows but _history_source_fp is not refreshed.
    with backend._lock:
        backend._messages.append(
            {"role": "assistant", "content": [{"type": "text", "text": "one more"}],
             "pid": "p-late"})
    events.clear()
    UserContext.reconcile_history_with_durable(ctx)
    kinds = [e.kind for e in events]
    # Rebuilt from durable: cache cleared and the full transcript re-emitted,
    # including the late message the stale cache never had.
    assert kinds[0] == "session_restart"
    assert "user_message_shown" in kinds
    assert any("one more" in (e.text or "") for e in events)


def test_invalidate_forces_rebuild_even_when_fingerprint_matches(
        tmp_path, monkeypatch, dek):
    # An offline write lands on disk without touching the in-memory list, so the
    # fingerprint can still match. invalidate_history_cache is the explicit
    # signal that forces the next reconcile to rebuild anyway.
    ctx, _b, events = _real_stack(tmp_path, monkeypatch, dek, [
        {"role": "user", "content": [{"type": "text", "text": "hi"}]},
    ])
    UserContext.invalidate_history_cache(ctx)
    assert ctx._history_source_fp is None
    events.clear()
    UserContext.reconcile_history_with_durable(ctx)
    assert any(e.kind == "session_restart" for e in events)


def test_broadcast_keeps_fingerprint_in_step(tmp_path, monkeypatch, dek):
    # After a normal history-appending broadcast the fingerprint tracks the
    # message list, so an in-sync reconnect stays free (no needless rebuild).
    ctx, backend, _events = _real_stack(tmp_path, monkeypatch, dek, [
        {"role": "user", "content": [{"type": "text", "text": "hi"}]},
    ])
    ctx._history_source_fp = None  # pretend unknown
    UserContext._broadcast(ctx, {"kind": "user_message_shown", "text": "hi"})
    assert ctx._history_source_fp == backend.history_fingerprint()
