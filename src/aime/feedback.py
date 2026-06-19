"""User feedback & error-report ticket store.

A small, cross-user store for two kinds of inbound message from the chat UI:

  * **feedback** — submitted deliberately via the "Send feedback" button.
  * **error**    — submitted when a user accepts the "Would you like to report
                   this error?" prompt after Aime surfaces a problem; the raw
                   error text rides along in ``detail`` so we can actually fix it.

Both land in the same ``tickets`` table and are triaged from the admin
dashboard's Feedback tab as a basic ticket system: each ticket has a ``status``
(open → in_progress → resolved) and an optional ``admin_note``.

It is cross-user, so — like :mod:`aime.topic_shares` and :mod:`aime.quota` — it
lives as one file at the database root (beside auth.sql) rather than inside any
one user's silo. Thread-safe via a single connection under a lock, the same
pattern as :class:`aime.quota.QuotaStore`; fine for a personal web app's
concurrency. Drop the file and this module to remove the feature.
"""

from __future__ import annotations

import os
import sqlite3
import threading


# The triage lifecycle. Deliberately tiny — this is a "basic ticket system",
# not a full issue tracker. Order here is the order shown in the dashboard.
STATUSES = ("open", "in_progress", "resolved")

# The two ways a ticket is created. Anything unrecognised is coerced to
# "feedback" on submit so a hand-crafted request can't wedge an odd kind in.
KINDS = ("feedback", "error")

# Defensive caps so a single submission can't bloat the store. The UI also
# limits the textarea, but never trust the client.
_MAX_MESSAGE = 4000
_MAX_DETAIL = 8000


def _clamp(text: str | None, limit: int) -> str | None:
    if text is None:
        return None
    text = text.strip()
    if not text:
        return None
    return text[:limit]


class FeedbackStore:
    """SQLite-backed ticket store. One file at the database root."""

    def __init__(self, db_path: str):
        self._db_path = db_path
        os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
        self._lock = threading.Lock()
        # check_same_thread=False + explicit lock: shared across Flask threads.
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._init_schema()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def _init_schema(self) -> None:
        with self._lock:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS tickets (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id     INTEGER,
                    username    TEXT,
                    kind        TEXT NOT NULL DEFAULT 'feedback',
                    message     TEXT NOT NULL,
                    detail      TEXT,
                    status      TEXT NOT NULL DEFAULT 'open',
                    admin_note  TEXT,
                    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
                    updated_at  TEXT NOT NULL DEFAULT (datetime('now'))
                );
                CREATE INDEX IF NOT EXISTS idx_tickets_status
                    ON tickets(status);
                """
            )
            self._conn.commit()

    # -- write path (chat UI) ------------------------------------------------

    def submit(self, *, user_id: int | None, username: str | None,
               kind: str, message: str, detail: str | None = None) -> int:
        """Record a new ticket and return its id. ``message`` is required; a
        blank one raises ValueError so the caller can 400. ``kind`` is coerced to
        a known value and ``detail`` (error text / page context) is optional."""
        message = _clamp(message, _MAX_MESSAGE)
        if not message:
            raise ValueError("message is required")
        if kind not in KINDS:
            kind = "feedback"
        detail = _clamp(detail, _MAX_DETAIL)
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO tickets (user_id, username, kind, message, detail) "
                "VALUES (?, ?, ?, ?, ?)",
                (user_id, username, kind, message, detail),
            )
            self._conn.commit()
            return int(cur.lastrowid)

    # -- read path (admin dashboard) -----------------------------------------

    def list(self, status: str | None = None) -> list[dict]:
        """All tickets, newest first. ``status`` filters to one lifecycle state
        when given (and recognised); otherwise every ticket is returned."""
        if status in STATUSES:
            rows = self._conn.execute(
                "SELECT * FROM tickets WHERE status = ? "
                "ORDER BY created_at DESC, id DESC",
                (status,),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM tickets ORDER BY created_at DESC, id DESC"
            ).fetchall()
        return [dict(r) for r in rows]

    def get(self, ticket_id: int) -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM tickets WHERE id = ?", (ticket_id,)
        ).fetchone()
        return dict(row) if row else None

    def counts(self) -> dict:
        """``{status: n, ..., 'total': n, 'unresolved': n}`` for the tab badge
        and the at-a-glance header."""
        rows = self._conn.execute(
            "SELECT status, COUNT(*) AS n FROM tickets GROUP BY status"
        ).fetchall()
        out = {s: 0 for s in STATUSES}
        for r in rows:
            out[r["status"]] = r["n"]
        out["total"] = sum(out[s] for s in STATUSES)
        out["unresolved"] = out["open"] + out["in_progress"]
        return out

    # -- triage (admin dashboard) --------------------------------------------

    def set_status(self, ticket_id: int, status: str) -> bool:
        """Move a ticket to a new lifecycle state. False on unknown status or
        missing ticket."""
        if status not in STATUSES:
            return False
        with self._lock:
            cur = self._conn.execute(
                "UPDATE tickets SET status = ?, updated_at = datetime('now') "
                "WHERE id = ?",
                (status, ticket_id),
            )
            self._conn.commit()
            return cur.rowcount > 0

    def set_note(self, ticket_id: int, note: str | None) -> bool:
        """Attach (or clear, with a blank) the admin triage note. False if the
        ticket doesn't exist."""
        note = _clamp(note, _MAX_DETAIL)
        with self._lock:
            cur = self._conn.execute(
                "UPDATE tickets SET admin_note = ?, updated_at = datetime('now') "
                "WHERE id = ?",
                (note, ticket_id),
            )
            self._conn.commit()
            return cur.rowcount > 0
