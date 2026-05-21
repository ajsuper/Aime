"""Account lifecycle: the permanent-purge end of soft deletion.

Soft delete itself lives in :mod:`aime.auth` — it just flags the ``users`` row
and leaves the account's data directory completely untouched, so the account
can be restored during a grace period. This module handles the other end:
permanently purging accounts whose grace period has expired.

A purge, per account, is three steps in a deliberate order:

1. take a final backup zip of the data directory (operator insurance against
   a buggy purge — this is *not* a user-facing recovery path),
2. hard-delete the ``users`` row (guarded by :meth:`auth.LocalAuthBackend.
   hard_delete` so only already-soft-deleted rows can ever go), then
3. remove the data directory.

Kept frontend-agnostic and free of CLI/argparse code so that
``scripts/manage_users.py`` is a thin wrapper over it.
"""

from __future__ import annotations

import datetime
import os
import shutil
from dataclasses import dataclass

from . import auth as _auth
from . import backup as _backup
from . import config


# Default grace period: how long a soft-deleted account is kept recoverable
# before it becomes eligible for purge.
DEFAULT_GRACE_DAYS = 30


def _user_dir(user_id: int, database_dir: str) -> str:
    # Mirrors aime.backup._user_dir and web_app._user_dir — the per-user data
    # directory holding database.sql, topics/, and conversations/.
    return os.path.join(database_dir, "users", str(user_id))


def _utcnow() -> datetime.datetime:
    """Naive UTC now, to compare against sqlite's ``datetime('now')`` output
    (which is also naive UTC)."""
    return datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)


def _parse_deleted_at(stamp: str | None) -> datetime.datetime:
    """Parse a ``deleted_at`` value. sqlite writes it via ``datetime('now')``
    as ``'YYYY-MM-DD HH:MM:SS'`` in UTC. An unparseable value is treated as
    "just now" so a malformed row is never purged early."""
    if not stamp:
        return _utcnow()
    try:
        return datetime.datetime.strptime(stamp, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return _utcnow()


@dataclass(frozen=True)
class PendingPurge:
    """A soft-deleted account as seen by the purge tooling."""

    user: _auth.UserRecord
    deleted_at: str | None
    days_deleted: int
    expired: bool  # True once days_deleted has reached the grace period


def list_pending(
    auth_backend: _auth.LocalAuthBackend,
    *,
    grace_days: int = DEFAULT_GRACE_DAYS,
) -> list[PendingPurge]:
    """Return every soft-deleted account, oldest first, annotated with how
    long it has been deleted and whether the grace period has expired."""
    now = _utcnow()
    pending: list[PendingPurge] = []
    for user in auth_backend.list_deleted_users():
        deleted_dt = _parse_deleted_at(user.deleted_at)
        days = max(0, (now - deleted_dt).days)
        pending.append(
            PendingPurge(
                user=user,
                deleted_at=user.deleted_at,
                days_deleted=days,
                expired=days >= grace_days,
            )
        )
    return pending


def purge_user(
    auth_backend: _auth.LocalAuthBackend,
    user_id: int,
    *,
    database_dir: str | None = None,
) -> str | None:
    """Permanently purge one soft-deleted account.

    Takes a final backup, hard-deletes the DB row, then removes the data
    directory. Returns the path of the backup zip (or ``None`` if the account
    had no data directory). Raises :class:`ValueError` if ``user_id`` is not a
    soft-deleted account — a live account can never be purged through here.
    """
    database_dir = database_dir or config.DATABASE_DIR

    # 1. Final backup before anything is removed. Lands under
    #    <database_dir>/backups/<id>/, which is outside the data directory, so
    #    step 3 does not delete it.
    backup_path = _backup.backup_user_data(
        user_id, database_dir=database_dir, reason="purge"
    )

    # 2. Hard-delete the row. The guard inside hard_delete() means this is
    #    where a "not actually soft-deleted" account is rejected — before we
    #    touch its data directory.
    if not auth_backend.hard_delete(user_id):
        raise ValueError(
            f"user {user_id} is not a soft-deleted account; refusing to purge"
        )

    # 3. Remove the data directory.
    shutil.rmtree(_user_dir(user_id, database_dir), ignore_errors=True)
    return backup_path


def purge_expired(
    auth_backend: _auth.LocalAuthBackend,
    *,
    grace_days: int = DEFAULT_GRACE_DAYS,
    database_dir: str | None = None,
    dry_run: bool = False,
) -> list[tuple[PendingPurge, str | None]]:
    """Purge every soft-deleted account past the grace period.

    Returns a list of ``(PendingPurge, backup_path)`` for the accounts acted
    on. With ``dry_run=True`` nothing is removed and every ``backup_path`` is
    ``None`` — the list just reports what *would* be purged.
    """
    results: list[tuple[PendingPurge, str | None]] = []
    for item in list_pending(auth_backend, grace_days=grace_days):
        if not item.expired:
            continue
        if dry_run:
            results.append((item, None))
        else:
            backup_path = purge_user(
                auth_backend, item.user.id, database_dir=database_dir
            )
            results.append((item, backup_path))
    return results
