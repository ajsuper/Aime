"""Tests for the topic-sharing grant store (``aime.topic_shares.ShareStore``).

The store *is* the access-control state for cross-user topic sharing: the web
layer authorizes every shared read/write by looking a grant up here. So the
behaviors that matter are the lifecycle transitions an attacker or a careless
caller could lean on — a declined offer must not silently become live access, a
revoke must take effect, an accepted share must not be quietly reset, and a bad
permission level must be rejected outright.

The controller-level routing guard is covered separately in
``test_shared_topic_guard``; this exercises the store underneath it.
"""

import pytest

from aime.topic_shares import (
    ShareStore,
    Share,
    InvalidPermission,
    PERM_VIEW,
    PERM_EDIT,
    STATUS_PENDING,
    STATUS_ACCEPTED,
    STATUS_DECLINED,
)


OWNER, TOPIC, RECIP = 1, 100, 2


@pytest.fixture
def store(tmp_path):
    s = ShareStore(str(tmp_path / "shares.sql"))
    yield s
    s.close()


# --------------------------------------------------------------------------- #
# share() — creation and the re-offer rules
# --------------------------------------------------------------------------- #
def test_new_share_starts_pending_with_permission(store):
    sh = store.share(OWNER, TOPIC, RECIP, PERM_EDIT)
    assert isinstance(sh, Share)
    assert (sh.owner_id, sh.topic_id, sh.recipient_id) == (OWNER, TOPIC, RECIP)
    assert sh.permission == PERM_EDIT
    assert sh.status == STATUS_PENDING
    assert sh.responded_at is None


def test_share_defaults_to_view(store):
    assert store.share(OWNER, TOPIC, RECIP).permission == PERM_VIEW


def test_declined_share_reoffered_returns_to_pending(store):
    store.share(OWNER, TOPIC, RECIP)
    assert store.respond(OWNER, TOPIC, RECIP, accept=False) is True
    assert store.get(OWNER, TOPIC, RECIP).status == STATUS_DECLINED
    # Re-sharing a declined grant must ask the recipient again, not auto-grant.
    re = store.share(OWNER, TOPIC, RECIP, PERM_EDIT)
    assert re.status == STATUS_PENDING
    assert re.permission == PERM_EDIT
    assert re.responded_at is None


def test_accepted_share_reshare_stays_accepted_and_updates_permission(store):
    store.share(OWNER, TOPIC, RECIP, PERM_VIEW)
    store.respond(OWNER, TOPIC, RECIP, accept=True)
    # Re-sharing an already-accepted topic must NOT bounce it back to pending
    # (that would silently drop a live partner's access); only permission moves.
    re = store.share(OWNER, TOPIC, RECIP, PERM_EDIT)
    assert re.status == STATUS_ACCEPTED
    assert re.permission == PERM_EDIT


def test_share_rejects_invalid_permission(store):
    with pytest.raises(InvalidPermission):
        store.share(OWNER, TOPIC, RECIP, "admin")
    # And nothing was written.
    assert store.get(OWNER, TOPIC, RECIP) is None


# --------------------------------------------------------------------------- #
# respond() — only acts on pending
# --------------------------------------------------------------------------- #
def test_respond_accepts_pending(store):
    store.share(OWNER, TOPIC, RECIP)
    assert store.respond(OWNER, TOPIC, RECIP, accept=True) is True
    sh = store.get(OWNER, TOPIC, RECIP)
    assert sh.status == STATUS_ACCEPTED
    assert sh.responded_at is not None


def test_respond_on_already_accepted_is_noop(store):
    store.share(OWNER, TOPIC, RECIP)
    store.respond(OWNER, TOPIC, RECIP, accept=True)
    # A second response can't flip an accepted grant — returns False, no change.
    assert store.respond(OWNER, TOPIC, RECIP, accept=False) is False
    assert store.get(OWNER, TOPIC, RECIP).status == STATUS_ACCEPTED


def test_respond_with_no_grant_is_false(store):
    assert store.respond(OWNER, TOPIC, 999, accept=True) is False


# --------------------------------------------------------------------------- #
# set_permission()
# --------------------------------------------------------------------------- #
def test_set_permission_on_existing(store):
    store.share(OWNER, TOPIC, RECIP, PERM_VIEW)
    assert store.set_permission(OWNER, TOPIC, RECIP, PERM_EDIT) is True
    assert store.get(OWNER, TOPIC, RECIP).permission == PERM_EDIT


def test_set_permission_missing_grant_is_false(store):
    assert store.set_permission(OWNER, TOPIC, RECIP, PERM_EDIT) is False


def test_set_permission_rejects_bad_level(store):
    store.share(OWNER, TOPIC, RECIP)
    with pytest.raises(InvalidPermission):
        store.set_permission(OWNER, TOPIC, RECIP, "root")


# --------------------------------------------------------------------------- #
# revoke paths — access removal
# --------------------------------------------------------------------------- #
def test_revoke_removes_grant(store):
    store.share(OWNER, TOPIC, RECIP)
    assert store.revoke(OWNER, TOPIC, RECIP) is True
    assert store.get(OWNER, TOPIC, RECIP) is None
    assert store.revoke(OWNER, TOPIC, RECIP) is False  # idempotent: already gone


def test_revoke_all_for_topic(store):
    store.share(OWNER, TOPIC, 2)
    store.share(OWNER, TOPIC, 3)
    store.share(OWNER, 200, 4)  # different topic, must survive
    assert store.revoke_all_for_topic(OWNER, TOPIC) == 2
    assert store.get(OWNER, TOPIC, 2) is None
    assert store.get(OWNER, 200, 4) is not None


def test_purge_user_removes_grants_in_both_directions(store):
    store.share(OWNER, TOPIC, 2)     # OWNER is owner
    store.share(5, 300, OWNER)       # OWNER is recipient
    store.share(7, 400, 8)           # unrelated, survives
    removed = store.purge_user(OWNER)
    assert removed == 2
    assert store.get(OWNER, TOPIC, 2) is None
    assert store.get(5, 300, OWNER) is None
    assert store.get(7, 400, 8) is not None


# --------------------------------------------------------------------------- #
# read queries used by the UI / authorization
# --------------------------------------------------------------------------- #
def test_get_is_none_when_absent(store):
    assert store.get(OWNER, TOPIC, RECIP) is None


def test_incoming_filters_by_status(store):
    store.share(OWNER, TOPIC, RECIP)              # pending
    store.share(OWNER, 200, RECIP)                # will accept
    store.respond(OWNER, 200, RECIP, accept=True)
    store.share(OWNER, 300, RECIP)                # will decline
    store.respond(OWNER, 300, RECIP, accept=False)

    all_in = store.incoming(RECIP)
    assert len(all_in) == 3
    accepted_pending = store.incoming(
        RECIP, statuses=(STATUS_ACCEPTED, STATUS_PENDING)
    )
    got = {(s.topic_id, s.status) for s in accepted_pending}
    assert got == {(TOPIC, STATUS_PENDING), (200, STATUS_ACCEPTED)}


def test_owner_shared_topic_ids_only_accepted(store):
    store.share(OWNER, TOPIC, 2)
    store.respond(OWNER, TOPIC, 2, accept=True)
    store.share(OWNER, 200, 3)  # pending only — not "shared with others" yet
    assert store.owner_shared_topic_ids(OWNER) == {TOPIC}


def test_topic_partners_only_accepted_recipients(store):
    store.share(OWNER, TOPIC, 2)
    store.respond(OWNER, TOPIC, 2, accept=True)
    store.share(OWNER, TOPIC, 3)  # still pending
    assert store.topic_partners(OWNER, TOPIC) == [2]


def test_for_topic_lists_all_grants(store):
    store.share(OWNER, TOPIC, 2)
    store.share(OWNER, TOPIC, 3)
    partners = {s.recipient_id for s in store.for_topic(OWNER, TOPIC)}
    assert partners == {2, 3}
