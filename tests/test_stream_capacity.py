"""Open SSE streams must not be able to starve the server's thread pool.

Every /stream connection parks one waitress worker thread for its whole life,
and a stream only learns its peer is gone when a write to the socket fails —
behind a buffering proxy that can take a very long time. Phone sleep/wake,
network changes and tab churn leave those behind, and once they outnumber the
pool *nothing* is served: new connections queue instead of running, so every
device shows an empty chat and sends hang, until enough drain and the backlog
arrives all at once.
"""

import queue
import threading

import pytest

import frontends.web_app as web_app


def _ctx():
    """A UserContext with just the SSE plumbing attach_client touches."""
    ctx = object.__new__(web_app.UserContext)
    ctx.user_id = 1
    ctx._history_lock = threading.Lock()
    ctx._subscribers_lock = threading.Lock()
    ctx._client_queues = []
    ctx._history = []
    ctx._history_seq = 0
    return ctx


def test_streams_under_the_cap_are_all_kept():
    ctx = _ctx()
    for _ in range(web_app.UserContext.MAX_CLIENT_STREAMS):
        ctx.attach_client()
    assert len(ctx._client_queues) == web_app.UserContext.MAX_CLIENT_STREAMS


def test_oldest_stream_is_evicted_past_the_cap():
    ctx = _ctx()
    first, _, _ = ctx.attach_client()
    for _ in range(web_app.UserContext.MAX_CLIENT_STREAMS):
        ctx.attach_client()

    # The pool stays bounded no matter how many connections churn through.
    assert len(ctx._client_queues) == web_app.UserContext.MAX_CLIENT_STREAMS
    assert first not in ctx._client_queues
    # And the evicted one is told to finish, so its thread is released now
    # rather than at its next failed write (which may never come).
    assert first.get_nowait() is web_app._STREAM_CLOSE


def test_eviction_does_not_disturb_the_new_stream():
    ctx = _ctx()
    for _ in range(web_app.UserContext.MAX_CLIENT_STREAMS + 3):
        newest, snapshot, head = ctx.attach_client()
    assert newest in ctx._client_queues
    with pytest.raises(queue.Empty):
        newest.get_nowait()
