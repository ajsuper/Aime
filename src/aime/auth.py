"""Authentication for the Aime web frontend.

Designed so the password backend can be swapped out later (OAuth, magic-link,
SSO) without changing the Flask layer. Anything that needs to know "who is the
current user" calls a small `AuthBackend` protocol; the default implementation
keeps username + Argon2-hashed-password rows in a sqlite database alongside
the user data directories.

Security choices, documented up front so the reasoning isn't lost:

* **Argon2id** for password hashing (`argon2-cffi`). Memory-hard, side-channel
  resistant, the current OWASP-recommended algorithm. Default parameters are
  the library's tuned defaults (~64 MiB, t=3, p=4) — overkill for a personal
  app but cheap to keep.
* **Constant-time verify on missing usernames**: a sentinel hash is verified
  when no user matches, so attackers can't tell "wrong password" from
  "unknown user" by timing.
* **Rate-limiting per username**: 5 failed attempts in 15 minutes locks the
  account for 15 minutes. Lock state lives in the DB so it survives restarts.
* **Username normalization**: stored case-insensitive (`COLLATE NOCASE`) so
  "Alice" and "alice" collide.

This module never touches Flask or HTTP — it only knows about user records.
The session layer is the caller's responsibility (see web_app.py).
"""

from __future__ import annotations

import hashlib
import os
import re
import secrets
import sqlite3
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError, InvalidHashError
from cryptography.exceptions import InvalidTag

from . import encryption as _enc


# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AccessKeyRecord:
    """An invite key as seen by admin tooling (the CLI today, a web dashboard
    later). The raw key is never stored or returned — only its sha256 hash —
    so this record identifies a key by a short hash prefix for display."""

    key_hash: str
    note: str
    created_at: str
    redeemed_by_id: int | None
    redeemed_by_username: str | None
    redeemed_at: str | None

    @property
    def redeemed(self) -> bool:
        return self.redeemed_by_id is not None


@dataclass(frozen=True)
class UserRecord:
    """The minimum a frontend needs to know about a logged-in user.

    `api_access` is the single source of truth for whether the user may send
    messages through the paid model backend. It is a persistent DB column —
    never recomputed from the deployment mode. AIME_ACCESS_MODE only decides
    the value stamped at signup and whether the /send gate is armed; see
    docs/access-control.md.

    `deleted_at` is NULL for an active account and a UTC timestamp string for
    a soft-deleted one (pending purge). It is only populated on records
    returned by `list_deleted_users()`; the normal account paths never surface
    soft-deleted rows at all.
    """

    id: int
    username: str
    api_access: bool = True
    deleted_at: str | None = None


class AuthError(Exception):
    """Base for everything the auth layer raises at a caller intentionally."""


class UsernameTaken(AuthError):
    pass


class InvalidCredentials(AuthError):
    """Raised by verify() — deliberately generic to avoid leaking which half
    of the (username, password) pair was wrong."""


class AccountLocked(AuthError):
    """Too many failed login attempts. The message includes seconds remaining."""

    def __init__(self, seconds_remaining: int):
        super().__init__(f"too many failed attempts; try again in {seconds_remaining}s")
        self.seconds_remaining = seconds_remaining


class AccountDeleted(AuthError):
    """Raised by verify() when the password is correct but the account has
    been soft-deleted. Carries the user id so the caller can offer recovery
    (the data is intact and restorable) rather than a bare "invalid
    credentials". Raised only *after* a successful password check, so it never
    leaks the existence of a deleted account to an attacker."""

    def __init__(self, user_id: int):
        super().__init__("account is deleted")
        self.user_id = user_id


class WeakPassword(AuthError):
    pass


class InvalidUsername(AuthError):
    pass


@runtime_checkable
class AuthBackend(Protocol):
    """Drop-in interface so we can swap LocalAuthBackend for OAuth/etc later
    without touching the Flask routes. Frontends should only depend on this.

    The access-control methods below are the single API surface for managing
    `api_access`. The admin CLI (scripts/access_keys.py) and any future admin
    web dashboard are both thin wrappers over these — no access logic should
    live in a frontend."""

    # ---- accounts & sessions ---------------------------------------------
    def create(
        self, username: str, password: str, api_access: bool = True
    ) -> tuple[UserRecord, bytes]: ...
    def verify(self, username: str, password: str) -> tuple[UserRecord, bytes]: ...
    def lookup(self, user_id: int) -> UserRecord | None: ...

    # ---- account lifecycle ----------------------------------------------
    # Soft delete / restore / permanent purge. scripts/manage_users.py and the
    # web frontend's account routes are thin wrappers over these.
    def soft_delete(self, user_id: int) -> bool: ...
    def restore(self, user_id: int, api_access: bool = False) -> bool: ...
    def hard_delete(self, user_id: int) -> bool: ...
    def list_deleted_users(self) -> list[UserRecord]: ...

    # ---- access control --------------------------------------------------
    # A future admin dashboard would call exactly these, behind an admin-only
    # route guard (see docs/access-control.md, "Future: admin dashboard").
    def set_api_access(self, user_id: int, allowed: bool) -> bool: ...
    def redeem_key(self, user_id: int, key: str) -> bool: ...
    def generate_access_key(self, note: str = "") -> str: ...
    def list_access_keys(self) -> list[AccessKeyRecord]: ...
    def revoke_access_key(self, key: str) -> bool: ...
    def list_users(self) -> list[UserRecord]: ...


# ---------------------------------------------------------------------------
# Local (username + password) implementation
# ---------------------------------------------------------------------------


_USERNAME_RE = re.compile(r"^[A-Za-z0-9_.\-]{3,32}$")
_MIN_PASSWORD_LEN = 8

# Tunables for the lockout policy.
_FAIL_WINDOW_SECONDS = 15 * 60
_FAIL_THRESHOLD = 5
_LOCK_SECONDS = 15 * 60

# A pre-computed Argon2 hash of an unguessable string. We verify against this
# whenever a username doesn't exist, so the request takes the same amount of
# time as a real check — no user-enumeration timing oracle.
_DUMMY_PASSWORD = "y\x00d:\x07\x9bP\xa1\xa7\xc4\x9e\xfc\xb6\xc1\x0b\x80"


class LocalAuthBackend:
    """Username + Argon2-hashed-password store, persisted to its own sqlite
    file. Thread-safe via a single connection guarded by a lock — fine for the
    expected concurrency of a personal web app."""

    def __init__(self, db_path: str):
        self._db_path = db_path
        os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
        self._lock = threading.Lock()
        self._hasher = PasswordHasher()
        # `check_same_thread=False` + an explicit lock is the standard
        # workaround for sharing a connection across Flask worker threads.
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._init_schema()
        # Pre-hash a constant dummy password so unknown-user verifies cost
        # the same as real ones.
        self._dummy_hash = self._hasher.hash(_DUMMY_PASSWORD)

    def _init_schema(self) -> None:
        with self._lock:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS users (
                    id            INTEGER PRIMARY KEY AUTOINCREMENT,
                    username      TEXT    NOT NULL UNIQUE COLLATE NOCASE,
                    password_hash TEXT    NOT NULL,
                    salt_kek      BLOB,
                    wrapped_dek   BLOB,
                    enc_version   INTEGER NOT NULL DEFAULT 0,
                    created_at    TEXT    NOT NULL DEFAULT (datetime('now')),
                    -- NULL = active. A UTC timestamp = soft-deleted and
                    -- pending purge; see the account-lifecycle methods.
                    deleted_at    TEXT
                );
                CREATE TABLE IF NOT EXISTS auth_attempts (
                    username      TEXT    PRIMARY KEY COLLATE NOCASE,
                    fail_count    INTEGER NOT NULL DEFAULT 0,
                    first_fail_at INTEGER,
                    locked_until  INTEGER
                );
                -- Invite keys for AIME_ACCESS_MODE=keys. Only the sha256 hash
                -- of each key is stored; the raw key is shown once at
                -- generation and never persisted. An unredeemed key has
                -- redeemed_by IS NULL; redeeming one is a single use.
                CREATE TABLE IF NOT EXISTS access_keys (
                    key_hash    TEXT    PRIMARY KEY,
                    note        TEXT    NOT NULL DEFAULT '',
                    created_at  TEXT    NOT NULL DEFAULT (datetime('now')),
                    redeemed_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
                    redeemed_at TEXT
                );
                """
            )
            # LEGACY MIGRATION — pre-encryption databases predate the
            # salt_kek/wrapped_dek/enc_version columns. ADD COLUMN them in
            # place so existing installs keep working. Safe to remove once
            # all live installs have been upgraded past this version.
            existing_cols = {
                row[1]
                for row in self._conn.execute("PRAGMA table_info(users)")
            }
            for col, decl in (
                ("salt_kek",    "BLOB"),
                ("wrapped_dek", "BLOB"),
                ("enc_version", "INTEGER NOT NULL DEFAULT 0"),
            ):
                if col not in existing_cols:
                    self._conn.execute(f"ALTER TABLE users ADD COLUMN {col} {decl}")
            # END LEGACY MIGRATION

            # api_access MIGRATION — added with the access-control feature.
            # DEFAULT 1 deliberately grandfathers every account that exists at
            # migration time: before this feature everyone could send, so
            # they keep that. New accounts created after the feature ships set
            # the value explicitly in create() based on AIME_ACCESS_MODE.
            if "api_access" not in existing_cols:
                self._conn.execute(
                    "ALTER TABLE users ADD COLUMN api_access INTEGER NOT NULL DEFAULT 1"
                )
            # END api_access MIGRATION

            # soft-delete MIGRATION — deleted_at added with the account
            # deletion feature. A bare ADD COLUMN defaults every existing row
            # to NULL, which is exactly "active", so all current accounts are
            # grandfathered in untouched.
            if "deleted_at" not in existing_cols:
                self._conn.execute(
                    "ALTER TABLE users ADD COLUMN deleted_at TEXT"
                )
            # END soft-delete MIGRATION
            self._conn.commit()

    # ---- AuthBackend interface --------------------------------------------

    def create(
        self, username: str, password: str, api_access: bool = True
    ) -> tuple[UserRecord, bytes]:
        """Create a new account. `api_access` is the value stamped into the
        new row: callers pass True for AIME_ACCESS_MODE=open and False for
        =keys (see web_app.py). It is plain persistent state from here on."""
        self._validate_username(username)
        self._validate_password(password)
        pw_hash = self._hasher.hash(password)

        # Generate this user's per-account data key and wrap it under a KEK
        # derived from the password. We hand the raw DEK back to the caller
        # so it can encrypt the new user's first conversation immediately;
        # only the wrapped form ever touches disk.
        salt_kek = _enc.generate_salt()
        dek = _enc.generate_dek()
        kek = _enc.derive_kek(password, salt_kek)
        wrapped = _enc.wrap_dek(kek, dek)

        with self._lock:
            try:
                cur = self._conn.execute(
                    "INSERT INTO users "
                    "(username, password_hash, salt_kek, wrapped_dek, enc_version, "
                    "api_access) "
                    "VALUES (?, ?, ?, ?, 1, ?)",
                    (username, pw_hash, salt_kek, wrapped, 1 if api_access else 0),
                )
                self._conn.commit()
            except sqlite3.IntegrityError as e:
                raise UsernameTaken(f"username already taken: {username!r}") from e
            return (
                UserRecord(id=cur.lastrowid, username=username, api_access=api_access),
                dek,
            )

    def verify(self, username: str, password: str) -> tuple[UserRecord, bytes]:
        # Check lockout up front so a locked account never advances to a
        # hash verify (saves CPU and ensures the error is consistent).
        self._raise_if_locked(username)

        with self._lock:
            row = self._conn.execute(
                "SELECT id, username, password_hash, salt_kek, wrapped_dek, "
                "enc_version, api_access, deleted_at "
                "FROM users WHERE username = ?",
                (username,),
            ).fetchone()

        if row is None:
            # Run a verify against the dummy hash anyway so the timing of
            # unknown-user vs wrong-password is indistinguishable.
            try:
                self._hasher.verify(self._dummy_hash, password)
            except VerifyMismatchError:
                pass
            self._register_failure(username)
            raise InvalidCredentials("invalid username or password")

        (user_id, stored_username, pw_hash, salt_kek, wrapped_dek,
         enc_version, api_access, deleted_at) = row
        try:
            self._hasher.verify(pw_hash, password)
        except (VerifyMismatchError, InvalidHashError):
            self._register_failure(username)
            raise InvalidCredentials("invalid username or password")

        # Successful verify. Opportunistically re-hash if argon2 has updated
        # its defaults — preserves forward security as parameters tighten.
        if self._hasher.check_needs_rehash(pw_hash):
            new_hash = self._hasher.hash(password)
            with self._lock:
                self._conn.execute(
                    "UPDATE users SET password_hash = ? WHERE id = ?",
                    (new_hash, user_id),
                )
                self._conn.commit()

        # Unwrap (or, for legacy users, mint) this account's DEK.
        if enc_version == 0 or salt_kek is None or wrapped_dek is None:
            # LEGACY MIGRATION — account predates encryption. The password
            # we just verified is the only opportunity to derive a KEK for
            # this user, so do it now. Safe to remove once no rows have
            # enc_version = 0.
            salt_kek = _enc.generate_salt()
            dek = _enc.generate_dek()
            kek = _enc.derive_kek(password, salt_kek)
            wrapped_dek = _enc.wrap_dek(kek, dek)
            with self._lock:
                self._conn.execute(
                    "UPDATE users SET salt_kek = ?, wrapped_dek = ?, enc_version = 1 "
                    "WHERE id = ?",
                    (salt_kek, wrapped_dek, user_id),
                )
                self._conn.commit()
            # END LEGACY MIGRATION
        else:
            kek = _enc.derive_kek(password, bytes(salt_kek))
            try:
                dek = _enc.unwrap_dek(kek, bytes(wrapped_dek))
            except InvalidTag:
                # Should never happen on a successful password verify — the
                # KEK was derived from the same password the wrap used. If
                # we see it, the row is corrupt; treat as auth failure.
                self._register_failure(username)
                raise InvalidCredentials("invalid username or password")

        self._clear_failures(username)

        # The password is correct. If the account has been soft-deleted, do
        # not hand back a session — signal the caller so it can offer account
        # recovery. Checked here, after the password verify, so a deleted
        # account is indistinguishable from a live one to anyone who does not
        # already know the password.
        if deleted_at is not None:
            raise AccountDeleted(user_id)

        return (
            UserRecord(
                id=user_id,
                username=stored_username,
                api_access=bool(api_access),
            ),
            dek,
        )

    def lookup(self, user_id: int) -> UserRecord | None:
        # Soft-deleted accounts are treated as gone here: a session cookie
        # that outlived a deletion resolves to None and the caller re-routes
        # to login (where recovery is offered).
        with self._lock:
            row = self._conn.execute(
                "SELECT id, username, api_access FROM users "
                "WHERE id = ? AND deleted_at IS NULL",
                (user_id,),
            ).fetchone()
        if row is None:
            return None
        return UserRecord(id=row[0], username=row[1], api_access=bool(row[2]))

    # ---- Account lifecycle ------------------------------------------------
    #
    # Soft delete flags a row (deleted_at = UTC timestamp) instead of removing
    # it: the account stops working and disappears from every normal listing
    # and lookup, but the user's data directory is left untouched so it can be
    # restored during the grace period. hard_delete() is the permanent removal
    # and is guarded so it can only ever touch an already-soft-deleted row.
    # aime.accounts orchestrates the timed purge; scripts/manage_users.py and
    # the web account routes wrap these.

    def soft_delete(self, user_id: int) -> bool:
        """Mark an account soft-deleted. Returns False if it does not exist or
        is already deleted."""
        with self._lock:
            cur = self._conn.execute(
                "UPDATE users SET deleted_at = datetime('now') "
                "WHERE id = ? AND deleted_at IS NULL",
                (user_id,),
            )
            self._conn.commit()
        return cur.rowcount > 0

    def soft_delete_by_username(self, username: str) -> bool:
        """Username-keyed soft delete for admin tooling."""
        with self._lock:
            cur = self._conn.execute(
                "UPDATE users SET deleted_at = datetime('now') "
                "WHERE username = ? AND deleted_at IS NULL",
                (username,),
            )
            self._conn.commit()
        return cur.rowcount > 0

    def restore(self, user_id: int, api_access: bool = False) -> bool:
        """Clear the soft-delete flag, reactivating the account. `api_access`
        is re-stamped on restore (mirroring create()): callers pass the value
        appropriate for their deployment mode — True in `open`, False in
        `keys` (the default, which forces a fresh key redemption). Returns
        False if there is no matching soft-deleted row."""
        with self._lock:
            cur = self._conn.execute(
                "UPDATE users SET deleted_at = NULL, api_access = ? "
                "WHERE id = ? AND deleted_at IS NOT NULL",
                (1 if api_access else 0, user_id),
            )
            self._conn.commit()
        return cur.rowcount > 0

    def restore_by_username(self, username: str, api_access: bool = False) -> bool:
        """Username-keyed restore for admin tooling. Defaults to api_access=False
        so admin-driven restores behave like keys-mode: an admin can grant
        access explicitly afterwards via scripts/access_keys.py."""
        with self._lock:
            cur = self._conn.execute(
                "UPDATE users SET deleted_at = NULL, api_access = ? "
                "WHERE username = ? AND deleted_at IS NOT NULL",
                (1 if api_access else 0, username),
            )
            self._conn.commit()
        return cur.rowcount > 0

    def hard_delete(self, user_id: int) -> bool:
        """Permanently remove an account row. Guarded: only ever deletes a row
        that is *already* soft-deleted, so a live account can never be purged
        by a stray call. Returns False if there is no soft-deleted row to
        remove. The caller (aime.accounts.purge_user) owns the data directory
        and the final backup."""
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM users WHERE id = ? AND deleted_at IS NOT NULL",
                (user_id,),
            )
            self._conn.commit()
        return cur.rowcount > 0

    def list_deleted_users(self) -> list[UserRecord]:
        """Every soft-deleted account, oldest deletion first. The returned
        records carry the deleted_at timestamp; for admin/purge tooling."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, username, api_access, deleted_at FROM users "
                "WHERE deleted_at IS NOT NULL ORDER BY deleted_at"
            ).fetchall()
        return [
            UserRecord(id=r[0], username=r[1], api_access=bool(r[2]),
                       deleted_at=r[3])
            for r in rows
        ]

    # ---- Access control ---------------------------------------------------
    #
    # Single API surface for managing `api_access`. scripts/access_keys.py and
    # any future admin web dashboard call exactly these methods; no access
    # logic lives in a frontend. All key lookups are by sha256 hash — the raw
    # key is never stored. Keys carry ~192 bits of entropy (token_urlsafe(24)),
    # so redemption is not brute-forceable and needs no rate limiting.

    @staticmethod
    def _hash_key(key: str) -> str:
        return hashlib.sha256(key.strip().encode("utf-8")).hexdigest()

    def set_api_access(self, user_id: int, allowed: bool) -> bool:
        """Directly grant (True) or revoke (False) a user's send access.
        This is the admin override and the future billing/over-limit hook.
        Returns False if no such user."""
        with self._lock:
            cur = self._conn.execute(
                "UPDATE users SET api_access = ? WHERE id = ?",
                (1 if allowed else 0, user_id),
            )
            self._conn.commit()
        return cur.rowcount > 0

    def set_api_access_by_username(self, username: str, allowed: bool) -> bool:
        """Username-keyed variant for admin tooling (CLI grant/revoke)."""
        with self._lock:
            cur = self._conn.execute(
                "UPDATE users SET api_access = ? WHERE username = ?",
                (1 if allowed else 0, username),
            )
            self._conn.commit()
        return cur.rowcount > 0

    def revoke_all_access(self) -> int:
        """Set api_access = 0 for every user. Intended for the billing
        cutover (zero the slate, then let billing re-grant on payment).
        Returns the number of rows affected."""
        with self._lock:
            cur = self._conn.execute("UPDATE users SET api_access = 0")
            self._conn.commit()
        return cur.rowcount

    def generate_access_key(self, note: str = "") -> str:
        """Mint a new single-use invite key, store its hash, and return the
        raw key. The raw value is shown to the admin once here and is then
        unrecoverable."""
        key = secrets.token_urlsafe(24)
        with self._lock:
            self._conn.execute(
                "INSERT INTO access_keys (key_hash, note) VALUES (?, ?)",
                (self._hash_key(key), note or ""),
            )
            self._conn.commit()
        return key

    def redeem_key(self, user_id: int, key: str) -> bool:
        """Redeem an unused key for `user_id`: marks the key consumed and sets
        the user's api_access = 1, atomically. Returns False if the key is
        unknown or already redeemed. Safe to call in any access mode."""
        key_hash = self._hash_key(key)
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM access_keys "
                "WHERE key_hash = ? AND redeemed_by IS NULL",
                (key_hash,),
            ).fetchone()
            if row is None:
                return False
            self._conn.execute(
                "UPDATE access_keys SET redeemed_by = ?, "
                "redeemed_at = datetime('now') WHERE key_hash = ?",
                (user_id, key_hash),
            )
            self._conn.execute(
                "UPDATE users SET api_access = 1 WHERE id = ?", (user_id,)
            )
            self._conn.commit()
        return True

    def revoke_access_key(self, key: str) -> bool:
        """Delete an as-yet-unredeemed key so it can never be used. Returns
        False if the key is unknown or already redeemed (redeeming is the
        permanent record; revoke a *user* instead via set_api_access)."""
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM access_keys "
                "WHERE key_hash = ? AND redeemed_by IS NULL",
                (self._hash_key(key),),
            )
            self._conn.commit()
        return cur.rowcount > 0

    def revoke_access_key_by_hash(self, key_hash: str) -> bool:
        """Hash-keyed variant of revoke_access_key. The raw key is never
        stored, so admin tooling that lists keys only ever has the hash to
        act on (e.g. the web admin dashboard). Same single-use semantics:
        only an unredeemed key can be revoked."""
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM access_keys "
                "WHERE key_hash = ? AND redeemed_by IS NULL",
                (key_hash,),
            )
            self._conn.commit()
        return cur.rowcount > 0

    def list_access_keys(self) -> list[AccessKeyRecord]:
        """Every key, redeemed and not, newest last. For admin listings."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT k.key_hash, k.note, k.created_at, "
                "k.redeemed_by, u.username, k.redeemed_at "
                "FROM access_keys k "
                "LEFT JOIN users u ON u.id = k.redeemed_by "
                "ORDER BY k.created_at"
            ).fetchall()
        return [
            AccessKeyRecord(
                key_hash=r[0],
                note=r[1] or "",
                created_at=r[2],
                redeemed_by_id=r[3],
                redeemed_by_username=r[4],
                redeemed_at=r[5],
            )
            for r in rows
        ]

    def list_users(self) -> list[UserRecord]:
        """Every active account with its current api_access. Soft-deleted
        accounts are excluded — see list_deleted_users(). For admin listings."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, username, api_access FROM users "
                "WHERE deleted_at IS NULL ORDER BY id"
            ).fetchall()
        return [
            UserRecord(id=r[0], username=r[1], api_access=bool(r[2]))
            for r in rows
        ]

    # ---- Validation -------------------------------------------------------

    @staticmethod
    def _validate_username(username: str) -> None:
        if not isinstance(username, str) or not _USERNAME_RE.match(username):
            raise InvalidUsername(
                "username must be 3-32 characters, letters/digits/._- only"
            )

    @staticmethod
    def _validate_password(password: str) -> None:
        if not isinstance(password, str) or len(password) < _MIN_PASSWORD_LEN:
            raise WeakPassword(
                f"password must be at least {_MIN_PASSWORD_LEN} characters"
            )

    # ---- Lockout bookkeeping ---------------------------------------------

    def _raise_if_locked(self, username: str) -> None:
        now = int(time.time())
        with self._lock:
            row = self._conn.execute(
                "SELECT locked_until FROM auth_attempts WHERE username = ?",
                (username,),
            ).fetchone()
        if row and row[0] and row[0] > now:
            raise AccountLocked(seconds_remaining=row[0] - now)

    def _register_failure(self, username: str) -> None:
        now = int(time.time())
        with self._lock:
            row = self._conn.execute(
                "SELECT fail_count, first_fail_at FROM auth_attempts "
                "WHERE username = ?",
                (username,),
            ).fetchone()
            if row is None or row[1] is None or now - row[1] > _FAIL_WINDOW_SECONDS:
                # First failure in a fresh window — reset the counter.
                fail_count, first_fail_at = 1, now
            else:
                fail_count, first_fail_at = row[0] + 1, row[1]
            locked_until = (now + _LOCK_SECONDS) if fail_count >= _FAIL_THRESHOLD else None
            self._conn.execute(
                """
                INSERT INTO auth_attempts (username, fail_count, first_fail_at, locked_until)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(username) DO UPDATE SET
                    fail_count    = excluded.fail_count,
                    first_fail_at = excluded.first_fail_at,
                    locked_until  = excluded.locked_until
                """,
                (username, fail_count, first_fail_at, locked_until),
            )
            self._conn.commit()

    def _clear_failures(self, username: str) -> None:
        with self._lock:
            self._conn.execute(
                "DELETE FROM auth_attempts WHERE username = ?", (username,)
            )
            self._conn.commit()


# ---------------------------------------------------------------------------
# Session secret-key management
# ---------------------------------------------------------------------------


class IPRateLimiter:
    """In-memory sliding-window rate limiter keyed by IP. Used for signup so a
    single host can't churn out hundreds of accounts. Tiny enough we don't
    need a persistent store — restarts reset the window, which is acceptable
    because the threshold is small and the consequences (a few extra accounts)
    are bounded."""

    def __init__(self, limit: int, window_seconds: int):
        self._limit = limit
        self._window = window_seconds
        self._lock = threading.Lock()
        self._hits: dict[str, deque[float]] = {}

    def hit(self, key: str) -> bool:
        """Record an attempt; return True if it's allowed, False if over."""
        now = time.monotonic()
        cutoff = now - self._window
        with self._lock:
            q = self._hits.get(key)
            if q is None:
                q = deque()
                self._hits[key] = q
            while q and q[0] < cutoff:
                q.popleft()
            if len(q) >= self._limit:
                return False
            q.append(now)
            return True


def load_or_create_secret_key(path: str) -> bytes:
    """Persist Flask's signing key on disk so sessions survive restarts.

    Generated on first run with os.urandom(32). File mode 0600 so only the
    owning user can read it. Re-reading on every restart instead of
    regenerating is what keeps users logged in across deploys.
    """
    if os.path.exists(path):
        with open(path, "rb") as f:
            data = f.read()
        if len(data) >= 32:
            return data
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    key = os.urandom(32)
    # O_CREAT|O_WRONLY|O_TRUNC with 0600 in one shot — no readable window.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, key)
    finally:
        os.close(fd)
    return key
