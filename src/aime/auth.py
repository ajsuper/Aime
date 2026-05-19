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

import os
import re
import sqlite3
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError, InvalidHashError


# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class UserRecord:
    """The minimum a frontend needs to know about a logged-in user."""

    id: int
    username: str


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


class WeakPassword(AuthError):
    pass


class InvalidUsername(AuthError):
    pass


@runtime_checkable
class AuthBackend(Protocol):
    """Drop-in interface so we can swap LocalAuthBackend for OAuth/etc later
    without touching the Flask routes. Frontends should only depend on this."""

    def create(self, username: str, password: str) -> UserRecord: ...
    def verify(self, username: str, password: str) -> UserRecord: ...
    def lookup(self, user_id: int) -> UserRecord | None: ...


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
                    created_at    TEXT    NOT NULL DEFAULT (datetime('now'))
                );
                CREATE TABLE IF NOT EXISTS auth_attempts (
                    username      TEXT    PRIMARY KEY COLLATE NOCASE,
                    fail_count    INTEGER NOT NULL DEFAULT 0,
                    first_fail_at INTEGER,
                    locked_until  INTEGER
                );
                """
            )
            self._conn.commit()

    # ---- AuthBackend interface --------------------------------------------

    def create(self, username: str, password: str) -> UserRecord:
        self._validate_username(username)
        self._validate_password(password)
        pw_hash = self._hasher.hash(password)
        with self._lock:
            try:
                cur = self._conn.execute(
                    "INSERT INTO users (username, password_hash) VALUES (?, ?)",
                    (username, pw_hash),
                )
                self._conn.commit()
            except sqlite3.IntegrityError as e:
                raise UsernameTaken(f"username already taken: {username!r}") from e
            return UserRecord(id=cur.lastrowid, username=username)

    def verify(self, username: str, password: str) -> UserRecord:
        # Check lockout up front so a locked account never advances to a
        # hash verify (saves CPU and ensures the error is consistent).
        self._raise_if_locked(username)

        with self._lock:
            row = self._conn.execute(
                "SELECT id, username, password_hash FROM users WHERE username = ?",
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

        user_id, stored_username, pw_hash = row
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

        self._clear_failures(username)
        return UserRecord(id=user_id, username=stored_username)

    def lookup(self, user_id: int) -> UserRecord | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT id, username FROM users WHERE id = ?",
                (user_id,),
            ).fetchone()
        if row is None:
            return None
        return UserRecord(id=row[0], username=row[1])

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
