"""Topic sharing: who has granted whom access to which topic.

A topic is owned by exactly one user and physically lives in that user's data
silo (``users/<owner_id>/topics/<id>_*.md`` in the C++ backend). Sharing never
moves or copies that content. Instead this store records *grants* — rows that
say "owner O let recipient R see/edit topic T" — and the web layer enforces
them by fetching the topic through the owner's gateway after checking for a
matching, accepted grant. See docs and the web_app topic routes.

Design notes kept here so the reasoning isn't lost:

* **The server is the trust boundary.** A recipient never receives the owner's
  data key; the server reads on their behalf. Revocation is therefore just
  deleting a row — there is no key to rotate. This is also why turning on
  encryption-at-rest later changes nothing here: content is always keyed by the
  *data owner*, and the server is the sole decryptor for any authorized access.
* **Identity is the user id, never the username.** Usernames are the immutable
  login identity, but we still store the numeric id so a row keeps pointing at
  the right account regardless of how the UI later displays a person.
* **Own store, own file.** Kept separate from auth.sql so the feature is a
  clean unit (drop the file + this module to remove it). It can't FK into the
  users table as a result; the web layer validates that both parties exist
  before writing a row, which is sufficient — a stale row for a since-deleted
  account simply never resolves to an accessible topic.
"""

from __future__ import annotations

import os
import sqlite3
import threading
from dataclasses import dataclass


# Permission levels, lowest first. 'view' can read; 'edit' can also save.
PERM_VIEW = "view"
PERM_EDIT = "edit"
_PERMISSIONS = (PERM_VIEW, PERM_EDIT)

# Share lifecycle. A grant starts 'pending' (the recipient has been offered the
# topic but hasn't acted), becomes 'accepted' when they take it, or 'declined'
# if they refuse. Only 'accepted' grants ever resolve to real access.
STATUS_PENDING = "pending"
STATUS_ACCEPTED = "accepted"
STATUS_DECLINED = "declined"


class ShareError(Exception):
    """Base for sharing errors raised at a caller intentionally."""


class InvalidPermission(ShareError):
    pass


@dataclass(frozen=True)
class Share:
    """One grant: owner O shared topic T with recipient R at some permission.

    `topic_id` is the id within the *owner's* database — meaningless without
    `owner_id`. The web layer pairs the two into a composite handle
    (``"<owner_id>:<topic_id>"``) when talking to a recipient's client.
    """

    owner_id: int
    topic_id: int
    recipient_id: int
    permission: str
    status: str
    created_at: str
    responded_at: str | None = None


class ShareStore:
    """SQLite-backed grant store. Thread-safe via a single connection guarded
    by a lock — same pattern as the auth backend, fine for the concurrency of a
    personal web app."""

    def __init__(self, db_path: str):
        self._db_path = db_path
        os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
        self._lock = threading.Lock()
        # check_same_thread=False + explicit lock: shared across Flask threads.
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._init_schema()

    def _init_schema(self) -> None:
        with self._lock:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS topic_shares (
                    owner_id     INTEGER NOT NULL,
                    topic_id     INTEGER NOT NULL,
                    recipient_id INTEGER NOT NULL,
                    permission   TEXT    NOT NULL DEFAULT 'view',
                    status       TEXT    NOT NULL DEFAULT 'pending',
                    created_at   TEXT    NOT NULL DEFAULT (datetime('now')),
                    responded_at TEXT,
                    PRIMARY KEY (owner_id, topic_id, recipient_id)
                );
                -- Recipient-side lookups ("what's shared with me") are the hot
                -- path: run on every /topics list. Index them.
                CREATE INDEX IF NOT EXISTS idx_topic_shares_recipient
                    ON topic_shares(recipient_id, status);
                """
            )
            self._conn.commit()

    # ---- helpers ----------------------------------------------------------

    @staticmethod
    def _validate_permission(permission: str) -> str:
        if permission not in _PERMISSIONS:
            raise InvalidPermission(
                f"permission must be one of {_PERMISSIONS!r}"
            )
        return permission

    @staticmethod
    def _row_to_share(row: tuple) -> Share:
        return Share(
            owner_id=row[0], topic_id=row[1], recipient_id=row[2],
            permission=row[3], status=row[4], created_at=row[5],
            responded_at=row[6],
        )

    # ---- writes -----------------------------------------------------------

    def share(
        self, owner_id: int, topic_id: int, recipient_id: int,
        permission: str = PERM_VIEW,
    ) -> Share:
        """Create or re-offer a grant. If one already exists for this
        (owner, topic, recipient) triple, its permission is updated; a grant
        that was previously declined (or is being re-created) is reset to
        pending so the recipient is asked again. Re-sharing an already-accepted
        topic just changes the permission and leaves it accepted.

        Returns the resulting Share. Raises InvalidPermission on a bad level.
        """
        self._validate_permission(permission)
        with self._lock:
            existing = self._conn.execute(
                "SELECT status FROM topic_shares "
                "WHERE owner_id = ? AND topic_id = ? AND recipient_id = ?",
                (owner_id, topic_id, recipient_id),
            ).fetchone()
            if existing is not None and existing[0] == STATUS_ACCEPTED:
                # Keep an accepted share live; only the permission changes.
                self._conn.execute(
                    "UPDATE topic_shares SET permission = ? "
                    "WHERE owner_id = ? AND topic_id = ? AND recipient_id = ?",
                    (permission, owner_id, topic_id, recipient_id),
                )
            else:
                # New, pending, or previously-declined: (re-)offer as pending.
                self._conn.execute(
                    "INSERT INTO topic_shares "
                    "(owner_id, topic_id, recipient_id, permission, status, "
                    "responded_at) "
                    "VALUES (?, ?, ?, ?, 'pending', NULL) "
                    "ON CONFLICT(owner_id, topic_id, recipient_id) DO UPDATE SET "
                    "permission = excluded.permission, "
                    "status = 'pending', responded_at = NULL",
                    (owner_id, topic_id, recipient_id, permission),
                )
            self._conn.commit()
            row = self._conn.execute(
                "SELECT owner_id, topic_id, recipient_id, permission, status, "
                "created_at, responded_at FROM topic_shares "
                "WHERE owner_id = ? AND topic_id = ? AND recipient_id = ?",
                (owner_id, topic_id, recipient_id),
            ).fetchone()
        return self._row_to_share(row)

    def set_permission(
        self, owner_id: int, topic_id: int, recipient_id: int, permission: str
    ) -> bool:
        """Change an existing grant's permission. Returns False if there's no
        such grant. Raises InvalidPermission on a bad level."""
        self._validate_permission(permission)
        with self._lock:
            cur = self._conn.execute(
                "UPDATE topic_shares SET permission = ? "
                "WHERE owner_id = ? AND topic_id = ? AND recipient_id = ?",
                (permission, owner_id, topic_id, recipient_id),
            )
            self._conn.commit()
        return cur.rowcount > 0

    def respond(
        self, owner_id: int, topic_id: int, recipient_id: int, accept: bool
    ) -> bool:
        """Recipient accepts or declines a *pending* grant. Returns False if
        there is no pending grant to act on (already responded, or none)."""
        new_status = STATUS_ACCEPTED if accept else STATUS_DECLINED
        with self._lock:
            cur = self._conn.execute(
                "UPDATE topic_shares "
                "SET status = ?, responded_at = datetime('now') "
                "WHERE owner_id = ? AND topic_id = ? AND recipient_id = ? "
                "AND status = 'pending'",
                (new_status, owner_id, topic_id, recipient_id),
            )
            self._conn.commit()
        return cur.rowcount > 0

    def revoke(self, owner_id: int, topic_id: int, recipient_id: int) -> bool:
        """Remove a grant entirely (owner un-shares). Access stops on the next
        request — there's no cached key to invalidate. Returns False if there
        was no such grant."""
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM topic_shares "
                "WHERE owner_id = ? AND topic_id = ? AND recipient_id = ?",
                (owner_id, topic_id, recipient_id),
            )
            self._conn.commit()
        return cur.rowcount > 0

    def revoke_all_for_topic(self, owner_id: int, topic_id: int) -> int:
        """Drop every grant on one of the owner's topics (e.g. when the topic
        is deleted). Returns the number removed."""
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM topic_shares WHERE owner_id = ? AND topic_id = ?",
                (owner_id, topic_id),
            )
            self._conn.commit()
        return cur.rowcount

    def purge_user(self, user_id: int) -> int:
        """Drop every grant that names `user_id` as either party — used when an
        account is purged so no dangling grants survive. Returns rows removed."""
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM topic_shares "
                "WHERE owner_id = ? OR recipient_id = ?",
                (user_id, user_id),
            )
            self._conn.commit()
        return cur.rowcount

    # ---- reads ------------------------------------------------------------

    def get(
        self, owner_id: int, topic_id: int, recipient_id: int
    ) -> Share | None:
        """The single grant for this (owner, topic, recipient), or None. This
        is the authorization lookup the topic routes run on every shared
        access."""
        with self._lock:
            row = self._conn.execute(
                "SELECT owner_id, topic_id, recipient_id, permission, status, "
                "created_at, responded_at FROM topic_shares "
                "WHERE owner_id = ? AND topic_id = ? AND recipient_id = ?",
                (owner_id, topic_id, recipient_id),
            ).fetchone()
        return self._row_to_share(row) if row else None

    def for_topic(self, owner_id: int, topic_id: int) -> list[Share]:
        """Every grant on one of the owner's topics — drives the owner's
        "shared with" menu. Newest first."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT owner_id, topic_id, recipient_id, permission, status, "
                "created_at, responded_at FROM topic_shares "
                "WHERE owner_id = ? AND topic_id = ? "
                "ORDER BY created_at DESC",
                (owner_id, topic_id),
            ).fetchall()
        return [self._row_to_share(r) for r in rows]

    def incoming(
        self, recipient_id: int, *, statuses: tuple[str, ...] | None = None
    ) -> list[Share]:
        """Every grant offered to `recipient_id`, optionally filtered to the
        given statuses (e.g. accepted + pending for the topic list). Newest
        first."""
        q = (
            "SELECT owner_id, topic_id, recipient_id, permission, status, "
            "created_at, responded_at FROM topic_shares WHERE recipient_id = ?"
        )
        params: list = [recipient_id]
        if statuses:
            q += " AND status IN (" + ",".join("?" * len(statuses)) + ")"
            params.extend(statuses)
        q += " ORDER BY created_at DESC"
        with self._lock:
            rows = self._conn.execute(q, params).fetchall()
        return [self._row_to_share(r) for r in rows]

    def participants(self, owner_id: int, topic_id: int) -> list[int]:
        """User ids that should be told when a shared topic changes: the owner
        plus every recipient with an *accepted* grant. Used to fan out the
        live-refresh ping across the involved users."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT recipient_id FROM topic_shares "
                "WHERE owner_id = ? AND topic_id = ? AND status = 'accepted'",
                (owner_id, topic_id),
            ).fetchall()
        return [owner_id] + [r[0] for r in rows]
