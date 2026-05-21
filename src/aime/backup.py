"""Reusable per-user data backup.

Snapshots a user's data directory — ``<DATABASE_DIR>/users/<id>/`` — into a
timestamped zip under ``<DATABASE_DIR>/backups/<id>/``. On-disk state is copied
verbatim: conversations stay encrypted, and ``database.sql`` is snapshotted
through sqlite's online backup API so it is internally consistent even while
the C++ backend is mid-write.

Used today as the safety net taken before a data import (see web_app.py's
``/account/import``). Deliberately standalone and frontend-agnostic so the
planned scheduled-autobackup feature can reuse it: call ``backup_user_data()``
on a timer, then ``prune_backups()`` to bound disk use.
"""

from __future__ import annotations

import datetime
import os
import sqlite3
import tempfile
import zipfile

from . import config


_BACKUPS_DIRNAME = "backups"


def _user_dir(user_id: int, database_dir: str) -> str:
    return os.path.join(database_dir, "users", str(user_id))


def _backups_dir(user_id: int, database_dir: str) -> str:
    return os.path.join(database_dir, _BACKUPS_DIRNAME, str(user_id))


def snapshot_sqlite(src_path: str, dst_path: str) -> None:
    """Copy a sqlite database via the online backup API — a consistent
    snapshot even if another process holds it open and is writing."""
    src = sqlite3.connect(src_path)
    try:
        dst = sqlite3.connect(dst_path)
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()


def backup_user_data(
    user_id: int,
    *,
    database_dir: str | None = None,
    reason: str = "manual",
) -> str | None:
    """Snapshot user ``user_id``'s data directory into a timestamped zip.

    Returns the path to the created zip, or ``None`` if the user has no data
    directory yet (nothing to back up). ``reason`` is a short tag embedded in
    the filename (e.g. ``"import"``, ``"auto"``) for later identification.
    """
    database_dir = database_dir or config.DATABASE_DIR
    user_dir = _user_dir(user_id, database_dir)
    if not os.path.isdir(user_dir):
        return None

    backups_dir = _backups_dir(user_id, database_dir)
    os.makedirs(backups_dir, exist_ok=True)

    stamp = datetime.datetime.now().strftime("%Y%m%dT%H%M%S")
    safe_reason = "".join(c for c in reason if c.isalnum() or c in "-_") or "backup"
    zip_path = os.path.join(backups_dir, f"{stamp}-{safe_reason}.zip")

    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for root, _dirs, files in os.walk(user_dir):
            for name in files:
                abs_path = os.path.join(root, name)
                rel_path = os.path.relpath(abs_path, user_dir)
                if rel_path == "database.sql":
                    # Consistent snapshot rather than a raw copy of a file
                    # that may be mid-write.
                    with tempfile.NamedTemporaryFile(delete=False) as tmp:
                        tmp_path = tmp.name
                    try:
                        snapshot_sqlite(abs_path, tmp_path)
                        zf.write(tmp_path, rel_path)
                    finally:
                        os.unlink(tmp_path)
                else:
                    zf.write(abs_path, rel_path)

    return zip_path


def list_backups(user_id: int, *, database_dir: str | None = None) -> list[str]:
    """Return the user's backup zip paths, oldest first. Empty if none."""
    database_dir = database_dir or config.DATABASE_DIR
    backups_dir = _backups_dir(user_id, database_dir)
    if not os.path.isdir(backups_dir):
        return []
    paths = [
        os.path.join(backups_dir, n)
        for n in os.listdir(backups_dir)
        if n.endswith(".zip")
    ]
    paths.sort()
    return paths


def prune_backups(
    user_id: int,
    *,
    keep: int,
    database_dir: str | None = None,
) -> list[str]:
    """Delete all but the newest ``keep`` backups for the user. Returns the
    list of deleted paths. Intended for the future autobackup scheduler so its
    disk use stays bounded; unused by the manual import path."""
    if keep < 0:
        raise ValueError("keep must be >= 0")
    paths = list_backups(user_id, database_dir=database_dir)
    stale = paths[:-keep] if keep else paths
    for path in stale:
        try:
            os.unlink(path)
        except OSError:
            pass
    return stale
