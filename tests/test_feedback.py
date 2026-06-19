"""Unit tests for the feedback / error-report ticket store (aime.feedback).

Covers: a submitted ticket round-trips and starts 'open'; an unknown kind is
coerced to 'feedback'; a blank message is rejected; over-long message/detail are
clamped; listing is newest-first and status-filterable; counts roll up
unresolved correctly; status transitions validate; and notes set/clear.
"""

import pytest

from aime import feedback


@pytest.fixture
def store(tmp_path):
    s = feedback.FeedbackStore(str(tmp_path / "feedback.sql"))
    yield s
    s.close()


def test_submit_roundtrips_and_starts_open(store):
    tid = store.submit(user_id=1, username="ann", kind="feedback",
                       message="love it", detail=None)
    t = store.get(tid)
    assert t["username"] == "ann"
    assert t["kind"] == "feedback"
    assert t["message"] == "love it"
    assert t["status"] == "open"
    assert t["admin_note"] is None


def test_error_kind_carries_detail(store):
    tid = store.submit(user_id=2, username="bob", kind="error",
                       message="broke", detail="Traceback: boom")
    t = store.get(tid)
    assert t["kind"] == "error"
    assert t["detail"] == "Traceback: boom"


def test_unknown_kind_coerced_to_feedback(store):
    tid = store.submit(user_id=1, username="ann", kind="nonsense",
                       message="hi")
    assert store.get(tid)["kind"] == "feedback"


def test_blank_message_rejected(store):
    with pytest.raises(ValueError):
        store.submit(user_id=1, username="ann", kind="feedback", message="   ")


def test_message_and_detail_clamped(store):
    tid = store.submit(user_id=1, username="ann", kind="error",
                       message="x" * 9000, detail="y" * 20000)
    t = store.get(tid)
    assert len(t["message"]) == feedback._MAX_MESSAGE
    assert len(t["detail"]) == feedback._MAX_DETAIL


def test_list_newest_first_and_status_filter(store):
    a = store.submit(user_id=1, username="ann", kind="feedback", message="first")
    b = store.submit(user_id=2, username="bob", kind="error", message="second")
    ids = [t["id"] for t in store.list()]
    assert ids == [b, a]  # newest first
    store.set_status(a, "resolved")
    open_ids = [t["id"] for t in store.list("open")]
    assert open_ids == [b]
    assert [t["id"] for t in store.list("resolved")] == [a]


def test_counts_roll_up_unresolved(store):
    store.submit(user_id=1, username="ann", kind="feedback", message="a")
    b = store.submit(user_id=2, username="bob", kind="error", message="b")
    c = store.submit(user_id=3, username="cara", kind="feedback", message="c")
    store.set_status(b, "in_progress")
    store.set_status(c, "resolved")
    counts = store.counts()
    assert counts["open"] == 1
    assert counts["in_progress"] == 1
    assert counts["resolved"] == 1
    assert counts["total"] == 3
    assert counts["unresolved"] == 2


def test_set_status_validates(store):
    tid = store.submit(user_id=1, username="ann", kind="feedback", message="a")
    assert store.set_status(tid, "bogus") is False
    assert store.set_status(tid, "in_progress") is True
    assert store.get(tid)["status"] == "in_progress"
    assert store.set_status(999, "open") is False  # missing ticket


def test_set_note_sets_and_clears(store):
    tid = store.submit(user_id=1, username="ann", kind="feedback", message="a")
    assert store.set_note(tid, "looking into it") is True
    assert store.get(tid)["admin_note"] == "looking into it"
    assert store.set_note(tid, "   ") is True  # blank clears
    assert store.get(tid)["admin_note"] is None
    assert store.set_note(999, "x") is False  # missing ticket
