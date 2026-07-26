"""Drift reconciliation must repair the replay cache, never gut it.

`/stream` compares the SSE replay cache against the durable message list on
every connect and rebuilds on a mismatch. But the cache spans more than the
current backend session — it accumulates across the day's silent idle
rollovers — so a *shrinking* durable list is normal rather than drift:

  * an idle/day rollover swaps in a fresh, empty session;
  * a background compaction folds the oldest messages into a summary.

Rebuilding from either wipes transcript the user can still see. Worse, the
rebuild re-anchors the fingerprint, so the loss survives every reload — only a
process restart (which rebuilds the cache from the newest persisted session)
brought the chat back.
"""

import frontends.web_app as web_app


class _Backend:
    def __init__(self, fp):
        self.fp = fp

    def history_fingerprint(self):
        return self.fp


class _Controller:
    def __init__(self):
        self.resyncs = 0

    def resync_view(self):
        self.resyncs += 1


def _ctx(cached_fp, durable_fp):
    """A UserContext with just the fields reconciliation touches."""
    import threading
    ctx = object.__new__(web_app.UserContext)
    ctx._history_lock = threading.Lock()
    ctx._history_source_fp = cached_fp
    ctx._backend = _Backend(durable_fp)
    ctx.controller = _Controller()
    return ctx


def test_in_sync_does_not_resync():
    ctx = _ctx((4, "abc"), (4, "abc"))
    ctx.reconcile_history_with_durable()
    assert ctx.controller.resyncs == 0


def test_real_drift_still_rebuilds():
    # Same length or longer with a different digest = an event we missed.
    ctx = _ctx((4, "abc"), (6, "xyz"))
    ctx.reconcile_history_with_durable()
    assert ctx.controller.resyncs == 1


def test_empty_session_after_rollover_does_not_blank_the_chat():
    ctx = _ctx((12, "abc"), (0, "empty"))
    ctx.reconcile_history_with_durable()
    assert ctx.controller.resyncs == 0
    # Re-anchored, so the next connect doesn't retry the same bad rebuild.
    assert ctx._history_source_fp == (0, "empty")


def test_compaction_does_not_truncate_the_view():
    ctx = _ctx((40, "abc"), (25, "xyz"))
    ctx.reconcile_history_with_durable()
    assert ctx.controller.resyncs == 0
    assert ctx._history_source_fp == (25, "xyz")


def test_unknown_fingerprint_rebuilds_when_there_is_something_to_rebuild_from():
    ctx = _ctx(None, (8, "abc"))
    ctx.reconcile_history_with_durable()
    assert ctx.controller.resyncs == 1


def test_unknown_fingerprint_on_an_empty_session_leaves_the_view_alone():
    ctx = _ctx(None, (0, "empty"))
    ctx.reconcile_history_with_durable()
    assert ctx.controller.resyncs == 0
