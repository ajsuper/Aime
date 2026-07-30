"""A connection dropped mid-turn is a network event, not a lost conversation.

Cloud hosts reset long-lived outbound TLS streams routinely (NAT/firewall idle
reapers), which surfaces as `[Errno 104] Connection reset by peer` while
iterating the model's SSE stream. The SDK's own retries don't apply once a
stream has started, so the turn used to die outright: the user got an error and
had to retype a message the model had already been paid to answer.
"""

import pytest

import aime.encryption as _enc
from provider_backend import AnthropicMessagesBackend, BackendEvent


@pytest.fixture
def backend(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    b = AnthropicMessagesBackend(
        system_prompt="sys", model="model", schema_files=[],
        conversations_dir=str(tmp_path), dek=_enc.generate_dek(),
    )
    b.new_session()
    # Retries shouldn't make the suite crawl.
    monkeypatch.setattr(type(b), "STREAM_RETRY_BACKOFF", (0.0, 0.0))
    return b


def _reset_error():
    """The exception exactly as it arrives: an OS reset wrapped by httpcore,
    re-wrapped by httpx, re-raised through the SDK's stream iterator."""
    os_err = ConnectionResetError(104, "Connection reset by peer")
    try:
        raise os_err
    except ConnectionResetError as inner:
        wrapped = type("ReadError", (Exception,), {})("[Errno 104] Connection reset by peer")
        wrapped.__cause__ = inner
        return wrapped


# --- detection --------------------------------------------------------------

def test_wrapped_reset_is_recognised_as_transient(backend):
    assert backend._is_transient_connection_error(_reset_error()) is True


def test_plain_reset_is_recognised(backend):
    assert backend._is_transient_connection_error(
        ConnectionResetError(104, "Connection reset by peer")) is True


def test_an_ordinary_bug_is_not_treated_as_transient(backend):
    assert backend._is_transient_connection_error(ValueError("bad input")) is False


# --- retry behaviour --------------------------------------------------------

def test_turn_retries_after_a_dropped_connection(backend, monkeypatch):
    calls = []

    def fake_run_turn():
        calls.append(1)
        if len(calls) == 1:
            raise _reset_error()
        yield BackendEvent(kind="assistant_send_text", text="the answer")
        yield BackendEvent(kind="turn_end", stop_reason="end_turn")

    monkeypatch.setattr(backend, "_run_turn", fake_run_turn)
    events = list(backend._turn_with_recovery())

    assert len(calls) == 2                                  # retried once
    assert [e.kind for e in events] == ["assistant_send_text", "turn_end"]
    assert "error" not in [e.kind for e in events]           # invisible to the user


def test_retry_gives_up_and_reports_after_the_attempt_budget(backend, monkeypatch):
    def always_drops():
        raise _reset_error()
        yield  # pragma: no cover - generator marker

    monkeypatch.setattr(backend, "_run_turn", always_drops)
    kinds = [e.kind for e in backend._turn_with_recovery()]

    # Still ends the turn, so the composer never wedges waiting for a turn_end.
    assert kinds == ["error", "turn_end"]


def test_no_retry_once_the_reply_has_started_streaming(backend, monkeypatch):
    """A retry re-runs the turn from the start, and the frontend accumulates
    assistant text across deltas — so re-streaming onto a partial reply would
    render it twice. Once output is visible, surface the error instead."""
    calls = []

    def drops_midway():
        calls.append(1)
        yield BackendEvent(kind="assistant_text_delta", text="half an ans")
        raise _reset_error()

    monkeypatch.setattr(backend, "_run_turn", drops_midway)
    kinds = [e.kind for e in backend._turn_with_recovery()]

    assert len(calls) == 1                                  # not retried
    assert kinds == ["assistant_text_delta", "error", "turn_end"]


def test_interrupt_during_the_gap_is_not_retried(backend, monkeypatch):
    def drops():
        raise _reset_error()
        yield  # pragma: no cover - generator marker

    monkeypatch.setattr(backend, "_run_turn", drops)
    backend._interrupted.set()
    kinds = [e.kind for e in backend._turn_with_recovery()]

    assert kinds == ["error", "turn_end"]


def test_a_non_transient_failure_is_reported_immediately(backend, monkeypatch):
    calls = []

    def boom():
        calls.append(1)
        raise ValueError("a real bug")
        yield  # pragma: no cover - generator marker

    monkeypatch.setattr(backend, "_run_turn", boom)
    kinds = [e.kind for e in backend._turn_with_recovery()]

    assert len(calls) == 1                                  # no pointless retries
    assert kinds == ["error", "turn_end"]
