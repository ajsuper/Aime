"""Tests for permanent account purge.

The guarantee under test is the one the Privacy Policy makes: once the grace
period expires and an account is purged, *nothing* of it is left behind — not
the row, not the data directory, and not any backup archive taken earlier in
the account's life. Purge deliberately keeps no safety copy, so these tests
are what stands between a code change and a silently-retained copy of data a
user asked us to erase.
"""

import os
import sqlite3

import pytest

from aime import accounts, backup
from aime.auth import LocalAuthBackend

_PW = "Sufficiently-long-pw-1"


@pytest.fixture
def env(tmp_path):
    """An auth backend plus the database dir its accounts live under."""
    database_dir = str(tmp_path)
    backend = LocalAuthBackend(os.path.join(database_dir, "auth.sql"))
    return backend, database_dir


def _user_dir(database_dir, user_id):
    return os.path.join(database_dir, "users", str(user_id))


def _still_exists(backend, user_id):
    """True while a row for `user_id` survives, soft-deleted or not.

    `lookup()` filters out soft-deleted rows, so it cannot tell "recoverable"
    from "purged" — the distinction these tests turn on.
    """
    if backend.lookup(user_id) is not None:
        return True
    return any(u.id == user_id for u in backend.list_deleted_users())


def _make_user_with_data(backend, database_dir, username="alice"):
    """Create an account and give it a data directory with something in it.

    `database.sql` has to be a real sqlite database: backup_user_data()
    snapshots it through sqlite's online backup API, which rejects a file that
    only looks like one.
    """
    user, _dek = backend.create(username, _PW)
    path = _user_dir(database_dir, user.id)
    os.makedirs(path, exist_ok=True)
    conn = sqlite3.connect(os.path.join(path, "database.sql"))
    try:
        conn.execute("CREATE TABLE notes (body TEXT)")
        conn.execute("INSERT INTO notes VALUES ('private user content')")
        conn.commit()
    finally:
        conn.close()
    return user


def test_purge_removes_row_and_data_directory(env):
    backend, database_dir = env
    user = _make_user_with_data(backend, database_dir)
    backend.soft_delete(user.id)

    accounts.purge_user(backend, user.id, database_dir=database_dir)

    assert not _still_exists(backend, user.id)
    assert not os.path.exists(_user_dir(database_dir, user.id))


def test_purge_writes_no_backup(env):
    """Purge must not leave a parting copy of the account behind — the whole
    point of deletion."""
    backend, database_dir = env
    user = _make_user_with_data(backend, database_dir)
    backend.soft_delete(user.id)

    accounts.purge_user(backend, user.id, database_dir=database_dir)

    assert backup.list_backups(user.id, database_dir=database_dir) == []


def test_purge_erases_pre_existing_backups(env):
    """Backups taken earlier (e.g. the /account/import safety net) live outside
    the data directory, so removing that directory does not reach them. Each is
    a full copy of the user's data and must go with the account."""
    backend, database_dir = env
    user = _make_user_with_data(backend, database_dir)

    made = backup.backup_user_data(
        user.id, database_dir=database_dir, reason="import"
    )
    assert made and os.path.exists(made), "fixture should have made a backup"

    backend.soft_delete(user.id)
    accounts.purge_user(backend, user.id, database_dir=database_dir)

    assert not os.path.exists(made)
    assert backup.list_backups(user.id, database_dir=database_dir) == []


def test_purge_refuses_a_live_account(env):
    """Only an already-soft-deleted account can be purged; a live one is
    rejected before any data is touched."""
    backend, database_dir = env
    user = _make_user_with_data(backend, database_dir)

    with pytest.raises(ValueError):
        accounts.purge_user(backend, user.id, database_dir=database_dir)

    assert _still_exists(backend, user.id)
    assert os.path.exists(_user_dir(database_dir, user.id))


def test_purge_expired_skips_accounts_inside_grace(env):
    """A just-deleted account is still recoverable and must survive a purge
    sweep."""
    backend, database_dir = env
    user = _make_user_with_data(backend, database_dir)
    backend.soft_delete(user.id)

    purged = accounts.purge_expired(
        backend, grace_days=30, database_dir=database_dir
    )

    assert purged == []
    assert _still_exists(backend, user.id)
    assert os.path.exists(_user_dir(database_dir, user.id))


def test_purge_expired_dry_run_removes_nothing(env):
    backend, database_dir = env
    user = _make_user_with_data(backend, database_dir)
    backend.soft_delete(user.id)

    # grace_days=0 makes it eligible immediately.
    reported = accounts.purge_expired(
        backend, grace_days=0, database_dir=database_dir, dry_run=True
    )

    assert [p.user.id for p in reported] == [user.id]
    assert _still_exists(backend, user.id)
    assert os.path.exists(_user_dir(database_dir, user.id))


def test_purge_expired_purges_past_grace(env):
    backend, database_dir = env
    user = _make_user_with_data(backend, database_dir)
    backend.soft_delete(user.id)

    purged = accounts.purge_expired(
        backend, grace_days=0, database_dir=database_dir
    )

    assert [p.user.id for p in purged] == [user.id]
    assert not _still_exists(backend, user.id)
    assert not os.path.exists(_user_dir(database_dir, user.id))
