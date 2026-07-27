"""Building one user's context must not block other users' requests.

A UserContext build does real I/O — an agent session, disk decryption, several
tool-gateway round trips with 10s timeouts each. It used to run while holding
the shared cache lock, so one slow build froze every other user's /send and
/stream. Invisible on a single-user instance; on a multi-user one it stalls
everybody, worst right after a restart when every client reconnects at once and
each build queues behind the last.
"""

import threading

import frontends.web_app as web_app


class _SlowContext:
    """Stands in for UserContext: user 1's build blocks until released."""

    gate = threading.Event()
    building = threading.Event()

    def __init__(self, user_id, username=None):
        self.user_id = user_id
        self.username = username
        if user_id == 1:
            self.building.set()
            assert self.gate.wait(10), "test gate never released"


def _clear_caches():
    web_app._user_contexts.clear()
    # getattr so the blocking assertion below is what fails on an
    # implementation without per-user build locks, rather than an AttributeError.
    getattr(web_app, "_user_context_build_locks", {}).clear()


def _fetch(user_id, into, index):
    with web_app.app.test_request_context():
        into[index] = web_app._context_for(user_id)


def test_a_slow_build_does_not_block_another_user(monkeypatch):
    monkeypatch.setattr(web_app, "UserContext", _SlowContext)
    _SlowContext.gate.clear()
    _SlowContext.building.clear()
    _clear_caches()

    results = {}
    slow = threading.Thread(target=_fetch, args=(1, results, "slow"), daemon=True)
    slow.start()
    assert _SlowContext.building.wait(5), "slow build never started"

    # User 1 is mid-build and holding its own lock. User 2 must sail past it.
    fast = threading.Thread(target=_fetch, args=(2, results, "fast"), daemon=True)
    fast.start()
    fast.join(timeout=5)
    assert not fast.is_alive(), "a second user was blocked by the first user's build"
    assert results["fast"].user_id == 2

    _SlowContext.gate.set()
    slow.join(timeout=5)
    assert results["slow"].user_id == 1
    _clear_caches()


def test_same_user_races_build_exactly_one_context(monkeypatch):
    built = []

    class _Counting:
        def __init__(self, user_id, username=None):
            self.user_id = user_id
            built.append(user_id)

    monkeypatch.setattr(web_app, "UserContext", _Counting)
    _clear_caches()

    results = {}
    threads = [threading.Thread(target=_fetch, args=(7, results, i), daemon=True)
               for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)

    assert built == [7], f"context built more than once: {built}"
    assert len({id(v) for v in results.values()}) == 1
    _clear_caches()
