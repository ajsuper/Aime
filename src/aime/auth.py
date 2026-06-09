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


# Current at-rest encryption scheme version. Bumped when the wrap layout
# changes (e.g. moving machine_secret into an OS keychain in the future).
#   0 = no encryption (very-pre-encryption accounts; never seen on fresh
#       installs but may exist on early beta databases).
#   1 = password-derived KEK (Argon2id). Removed; v1 rows are auto-scrubbed
#       on next verify — see LEGACY MIGRATION AUTH below.
#   2 = machine-secret-derived KEK (HKDF). Current.
_ENC_VERSION_CURRENT = 2


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
    email: str | None = None
    # Opaque destination for outbound messages (see aime.messaging): a Telegram
    # chat id today, a phone number once an SMS channel lands. NULL = the user
    # hasn't connected a messaging contact, so proactive messages are skipped.
    messaging_contact: str | None = None
    # Last-seen IANA timezone (e.g. "America/New_York"), persisted from the
    # browser on each /send so it self-corrects if the user travels. Interactive
    # sessions still drive "now" from the live per-request tz; this stored value
    # is the fallback for code that runs with no live client — chiefly background
    # agent runs. NULL until the user has sent at least one message.
    tz: str | None = None
    # How the user wants dates/times *displayed* — the patterns from the web
    # settings ("MM/DD/YYYY", "12"/"24", etc.; see aime.dateformat). The browser
    # resolves its "auto" option to a concrete value before sending, and
    # persists it here on each /send like `tz`. The stored value drives how the
    # model writes dates back to the user; NULL means "not set" and callers fall
    # back to an unambiguous default (see dateformat.default_date_format).
    date_format: str | None = None
    time_format: str | None = None
    # Display-only real name. Unlike `username` (the immutable identity that
    # keys every piece of the user's data) these are purely cosmetic, freely
    # changeable, and may be NULL. Nothing keys off them today; they exist so a
    # future UI can greet the user by name.
    first_name: str | None = None
    last_name: str | None = None

    @property
    def display_name(self) -> str:
        """Best human-facing label: the full real name if any was given, else
        the username. Callers that want to render "who is this" should prefer
        this over reaching for the raw fields."""
        full = " ".join(p for p in (self.first_name, self.last_name) if p)
        return full or self.username


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


class BackgroundUnavailable(AuthError):
    """Raised by `get_dek` when a user's row predates the current encryption
    scheme and so cannot be unwrapped without their password. Resolved by the
    user logging in via the web frontend once — that triggers the auto-upgrade
    to the current scheme. Until then, background services (Midnight) cannot
    act on the account."""

    def __init__(self, user_id: int):
        super().__init__(f"user {user_id} has not been upgraded to the "
                         "current encryption scheme")
        self.user_id = user_id


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


class InvalidEmail(AuthError):
    pass


class InvalidName(AuthError):
    """A display name (first/last) failed validation — currently only a length
    cap. Names are optional, so an empty value never raises this."""


class VerificationError(AuthError):
    """Wrong / expired / used-up verification code, or unknown token. Kept
    deliberately generic so the UI doesn't differentiate between "wrong code"
    and "code expired" in a way that helps an attacker probe state."""


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
        self, username: str, password: str, api_access: bool = True,
        *, first_name: str | None = None, last_name: str | None = None,
    ) -> tuple[UserRecord, bytes]: ...
    # Update the display-only first/last name on an existing account. The
    # username is never touched here — it's the immutable identity.
    def set_display_name(
        self, user_id: int,
        first_name: str | None, last_name: str | None,
    ) -> bool: ...
    # `was_reinitialized` is True when verify() upgraded a pre-existing v0/v1
    # account to the current encryption scheme. The DEK is fresh in that case,
    # so any conversation files previously on disk are now unreadable garbage
    # and the caller should wipe them. See LEGACY MIGRATION AUTH in
    # LocalAuthBackend.verify().
    def verify(
        self, username: str, password: str, *, ip: str | None = None,
    ) -> tuple[UserRecord, bytes, bool]: ...
    def lookup(self, user_id: int) -> UserRecord | None: ...
    # For background services (Midnight) that need a user's DEK without the
    # user being present to type a password. Raises BackgroundUnavailable for
    # accounts that haven't been upgraded yet.
    def get_dek(self, user_id: int) -> bytes: ...

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
# Upper bound on password length. Argon2 hashing cost scales with input size,
# so an unbounded password is a cheap CPU-exhaustion vector; 1024 is far above
# any real passphrase. (NIST 800-63B recommends accepting at least 64.)
_MAX_PASSWORD_LEN = 1024
# Display names are free-form (any script, spaces, punctuation) so we only
# bound the length — enough to stop abuse, generous enough for real names.
_MAX_NAME_LEN = 64

# Tunables for the lockout policy.
_FAIL_WINDOW_SECONDS = 15 * 60
_FAIL_THRESHOLD = 5
_LOCK_SECONDS = 15 * 60

# Auth-event retention. Rows older than this are dropped opportunistically on
# each write so the table can't grow without bound on a long-running host.
_AUTH_EVENT_TTL = 30 * 24 * 60 * 60  # 30 days

# Recognized event kinds. Kept as constants so the dashboard and the backend
# agree on the strings.
EVENT_LOGIN_UNKNOWN_USER  = "login_unknown_user"
EVENT_LOGIN_BAD_PASSWORD  = "login_bad_password"
EVENT_LOGIN_WHILE_LOCKED  = "login_while_locked"
EVENT_LOCKOUT_STARTED     = "lockout_started"
EVENT_SIGNUP_RATE_LIMITED = "signup_rate_limited"
EVENT_SIGNUP_FAILED       = "signup_failed"
EVENT_LOGIN_IP_THROTTLED  = "login_ip_throttled"
EVENT_PASSWORD_RESET      = "password_reset"

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
        # Root of trust for at-rest encryption. Lives next to auth.sql so the
        # auth backend can find it without callers passing a path. See
        # `encryption.load_or_create_machine_secret` for the file format and
        # the threat model.
        self._machine_secret = _enc.load_or_create_machine_secret(
            os.path.join(os.path.dirname(db_path) or ".", "machine_secret")
        )

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
                -- Auth-event audit log. Written on every failed login, lockout
                -- trip, signup throttle, and signup failure so the admin
                -- dashboard can surface abnormal patterns. `ts` is a unix
                -- timestamp (seconds). `username` and `ip` may be NULL when
                -- not applicable; `detail` is a short freeform note (e.g. a
                -- specific failure reason). Rows older than _AUTH_EVENT_TTL
                -- seconds are pruned opportunistically.
                CREATE TABLE IF NOT EXISTS auth_events (
                    id       INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts       INTEGER NOT NULL,
                    kind     TEXT    NOT NULL,
                    username TEXT,
                    ip       TEXT,
                    detail   TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_auth_events_ts
                    ON auth_events(ts DESC);
                CREATE INDEX IF NOT EXISTS idx_auth_events_kind_ts
                    ON auth_events(kind, ts DESC);
                -- Trusted-device tokens for "remember this device" — lets a
                -- device that has already passed login 2FA skip the emailed
                -- code on subsequent logins until the token expires. Only the
                -- sha256 of the token is stored; the raw token lives in a
                -- long-lived signed cookie on the device (see web_app.py). A
                -- row is deleted when it expires, when the user revokes it, or
                -- (via ON DELETE CASCADE) when the account is hard-deleted.
                CREATE TABLE IF NOT EXISTS trusted_devices (
                    token_hash   TEXT    PRIMARY KEY,
                    user_id      INTEGER NOT NULL
                                 REFERENCES users(id) ON DELETE CASCADE,
                    created_at   INTEGER NOT NULL,
                    expires_at   INTEGER NOT NULL,
                    last_used_at INTEGER,
                    user_agent   TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_trusted_devices_user
                    ON trusted_devices(user_id);
                """
            )
            # LEGACY MIGRATION AUTH — pre-encryption databases predate the
            # salt_kek/wrapped_dek/enc_version columns. ADD COLUMN them in
            # place so existing installs keep working. salt_kek/wrapped_dek
            # are also the old (v1) password-derived wrapping; the wrap path
            # itself is gone, but the columns stay so verify() can detect a
            # v1 row and trigger the auto-upgrade. Safe to remove once no
            # rows have enc_version < 2.
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
            # END LEGACY MIGRATION AUTH

            # v2 encryption columns. Populated for every active account on a
            # fresh install (create() always writes v2) and lazily for legacy
            # rows on next login (see LEGACY MIGRATION AUTH in verify()).
            for col, decl in (
                ("salt_dek",       "BLOB"),
                ("wrapped_dek_v2", "BLOB"),
            ):
                if col not in existing_cols:
                    self._conn.execute(f"ALTER TABLE users ADD COLUMN {col} {decl}")

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

            # email MIGRATION — added with the email 2FA feature. NULL means
            # "not yet collected" so pre-existing accounts are prompted for an
            # email + verification code on next login.
            if "email" not in existing_cols:
                self._conn.execute(
                    "ALTER TABLE users ADD COLUMN email TEXT"
                )
            # END email MIGRATION

            # messaging_contact MIGRATION — added with the outbound-messaging
            # feature (aime.messaging). NULL means the account has no messaging
            # destination connected yet. Channel-agnostic on purpose: holds a
            # Telegram chat id now, a phone number under a future SMS channel.
            # Dropping the feature is just dropping this column + its setter.
            if "messaging_contact" not in existing_cols:
                self._conn.execute(
                    "ALTER TABLE users ADD COLUMN messaging_contact TEXT"
                )
            # END messaging_contact MIGRATION

            # tz MIGRATION — last-seen IANA timezone, persisted from the browser
            # on each /send (see UserRecord.tz). NULL until the user's first
            # message. Used as the timezone fallback for runs with no live client
            # (background agents). Dropping it is just dropping this column + its
            # setter and the persist call in the /send handler.
            if "tz" not in existing_cols:
                self._conn.execute(
                    "ALTER TABLE users ADD COLUMN tz TEXT"
                )
            # END tz MIGRATION

            # date-prefs MIGRATION — how the user wants dates/times *displayed*
            # (see UserRecord.date_format / .time_format). Forwarded from the
            # browser on each /send, same as tz, so the model writes prose and
            # summaries in the user's format. NULL until first set; the live
            # client always sends a concrete value, so these are mainly the
            # fallback for runs with no client (background agents). Dropping the
            # feature is just dropping these columns + their setter + the persist
            # call in the /send handler.
            for col in ("date_format", "time_format"):
                if col not in existing_cols:
                    self._conn.execute(
                        f"ALTER TABLE users ADD COLUMN {col} TEXT"
                    )
            # END date-prefs MIGRATION

            # display-name MIGRATION — first_name/last_name added as purely
            # cosmetic fields. A bare ADD COLUMN defaults every existing row to
            # NULL, i.e. "no name given", which is exactly right: the UI falls
            # back to the username. Dropping the feature is just dropping these
            # two columns.
            for col in ("first_name", "last_name"):
                if col not in existing_cols:
                    self._conn.execute(
                        f"ALTER TABLE users ADD COLUMN {col} TEXT"
                    )
            # END display-name MIGRATION

            # Pending email-verification rows. Used for three flows:
            #   purpose='signup'    — username/password are being held until
            #                         the 6-digit code mailed to `email` is
            #                         confirmed; on confirm the row's data is
            #                         promoted into a real users row.
            #   purpose='add_email' — an existing user is attaching an email
            #                         to their account; on confirm the email
            #                         is written onto users.email.
            #   purpose='login'     — login-time 2FA: a correct password has
            #                         been seen and a code mailed to the
            #                         account's existing email; on confirm the
            #                         session is granted. Mutates nothing.
            # `token` is a 256-bit random secret kept in the user's signed
            # session — it's how a request proves it owns this pending row
            # without having to retype the username/email.
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS email_verifications (
                    token         TEXT    PRIMARY KEY,
                    purpose       TEXT    NOT NULL,
                    user_id       INTEGER,
                    username      TEXT    COLLATE NOCASE,
                    password_hash TEXT,
                    email         TEXT    NOT NULL,
                    code_hash     TEXT    NOT NULL,
                    api_access    INTEGER NOT NULL DEFAULT 0,
                    created_at    INTEGER NOT NULL,
                    expires_at    INTEGER NOT NULL,
                    attempts      INTEGER NOT NULL DEFAULT 0,
                    -- Display-only name held alongside the pending signup so it
                    -- can be written onto the real row when the code is
                    -- confirmed. NULL for 'add_email' rows (which don't touch
                    -- the name) and for pre-feature pending rows.
                    first_name    TEXT,
                    last_name     TEXT
                )
                """
            )
            # display-name MIGRATION (email_verifications) — same columns added
            # to a table that may predate the feature on an existing install.
            ev_cols = {
                row[1]
                for row in self._conn.execute(
                    "PRAGMA table_info(email_verifications)"
                )
            }
            for col in ("first_name", "last_name"):
                if col not in ev_cols:
                    self._conn.execute(
                        f"ALTER TABLE email_verifications ADD COLUMN {col} TEXT"
                    )
            # END display-name MIGRATION (email_verifications)
            self._conn.commit()

    # ---- AuthBackend interface --------------------------------------------

    def create(
        self, username: str, password: str, api_access: bool = True,
        *, first_name: str | None = None, last_name: str | None = None,
    ) -> tuple[UserRecord, bytes]:
        """Create a new account. `api_access` is the value stamped into the
        new row: callers pass True for AIME_ACCESS_MODE=open and False for
        =keys (see web_app.py). It is plain persistent state from here on.

        `first_name`/`last_name` are optional display-only fields (see
        UserRecord); they're normalized to NULL when blank."""
        self._validate_username(username)
        self._validate_password(password, username=username)
        first_name = self._validate_name(first_name)
        last_name = self._validate_name(last_name)
        pw_hash = self._hasher.hash(password)

        # Generate this user's per-account data key and wrap it under a KEK
        # derived from the host's machine secret. The password no longer plays
        # a role in encryption — it only authenticates. We hand the raw DEK
        # back to the caller so it can encrypt the new user's first
        # conversation immediately; only the wrapped form ever touches disk.
        salt_dek = _enc.generate_salt()
        dek = _enc.generate_dek()
        kek = _enc.derive_kek(self._machine_secret, salt_dek)
        wrapped = _enc.wrap_dek(kek, dek)

        with self._lock:
            try:
                cur = self._conn.execute(
                    "INSERT INTO users "
                    "(username, password_hash, salt_dek, wrapped_dek_v2, "
                    "enc_version, api_access, first_name, last_name) "
                    f"VALUES (?, ?, ?, ?, {_ENC_VERSION_CURRENT}, ?, ?, ?)",
                    (username, pw_hash, salt_dek, wrapped,
                     1 if api_access else 0, first_name, last_name),
                )
                self._conn.commit()
            except sqlite3.IntegrityError as e:
                raise UsernameTaken(f"username already taken: {username!r}") from e
            return (
                UserRecord(
                    id=cur.lastrowid, username=username, api_access=api_access,
                    first_name=first_name, last_name=last_name,
                ),
                dek,
            )

    def verify(
        self, username: str, password: str, *, ip: str | None = None,
    ) -> tuple[UserRecord, bytes, bool]:
        """Authenticate (username, password) and return the user's DEK.

        The third return value, `was_reinitialized`, is True when the row was
        a pre-v2 account that we just auto-upgraded — see LEGACY MIGRATION
        AUTH below. In that case the DEK is fresh, so any conversation files
        on disk are now unreadable garbage and the caller is responsible for
        wiping them.
        """
        # Check lockout up front so a locked account never advances to a
        # hash verify (saves CPU and ensures the error is consistent).
        self._raise_if_locked(username, ip=ip)

        with self._lock:
            row = self._conn.execute(
                "SELECT id, username, password_hash, salt_dek, wrapped_dek_v2, "
                "enc_version, api_access, deleted_at, email, messaging_contact, "
                "first_name, last_name "
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
            self._register_failure(username, ip=ip, unknown_user=(row is None))
            raise InvalidCredentials("invalid username or password")

        (user_id, stored_username, pw_hash, salt_dek, wrapped_dek_v2,
         enc_version, api_access, deleted_at, email, messaging_contact,
         first_name, last_name) = row
        try:
            self._hasher.verify(pw_hash, password)
        except (VerifyMismatchError, InvalidHashError):
            self._register_failure(username, ip=ip, unknown_user=(row is None))
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

        was_reinitialized = False
        if (enc_version != _ENC_VERSION_CURRENT
                or salt_dek is None or wrapped_dek_v2 is None):
            # LEGACY MIGRATION AUTH — account is on a pre-v2 encryption
            # scheme (v1 password-derived KEK, or v0 plaintext). Aime is in
            # early beta and existing conversation files are not worth the
            # complexity of carrying the old decrypt path forward, so we
            # mint a fresh v2 DEK and signal the caller to wipe the user's
            # conversations directory. The auth.sql row (preferences,
            # api_access, lockout history) is preserved untouched. Safe to
            # remove once no rows have enc_version < 2.
            dek, salt_dek, wrapped_dek_v2 = self._mint_v2_keys(user_id)
            was_reinitialized = True
            # END LEGACY MIGRATION AUTH
        else:
            kek = _enc.derive_kek(self._machine_secret, bytes(salt_dek))
            try:
                dek = _enc.unwrap_dek(kek, bytes(wrapped_dek_v2))
            except InvalidTag:
                # The wrap was made with a different machine_secret. Most
                # commonly: the secret file was deleted/regenerated. We
                # can't recover the old DEK, so treat this row the same as
                # a legacy upgrade — mint fresh keys and signal the wipe.
                dek, salt_dek, wrapped_dek_v2 = self._mint_v2_keys(user_id)
                was_reinitialized = True

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
                email=email,
                messaging_contact=messaging_contact,
                first_name=first_name,
                last_name=last_name,
            ),
            dek,
            was_reinitialized,
        )

    def get_dek(self, user_id: int) -> bytes:
        """Unwrap a user's DEK from the machine secret, no password required.

        For background services (Midnight) that need to act on a user's data
        while the user is offline. Raises `BackgroundUnavailable` for
        pre-v2 accounts — those don't get unwrapped here on principle, since
        we'd have no way to wipe their stale conversations without knowing
        the data directory layout. Such accounts upgrade on next login.
        """
        with self._lock:
            row = self._conn.execute(
                "SELECT salt_dek, wrapped_dek_v2, enc_version "
                "FROM users WHERE id = ? AND deleted_at IS NULL",
                (user_id,),
            ).fetchone()
        if row is None:
            raise BackgroundUnavailable(user_id)
        salt_dek, wrapped_dek_v2, enc_version = row
        if (enc_version != _ENC_VERSION_CURRENT
                or salt_dek is None or wrapped_dek_v2 is None):
            raise BackgroundUnavailable(user_id)
        kek = _enc.derive_kek(self._machine_secret, bytes(salt_dek))
        try:
            return _enc.unwrap_dek(kek, bytes(wrapped_dek_v2))
        except InvalidTag as e:
            # machine_secret was rotated/regenerated since this row was
            # written. Same recovery as verify(): the row needs to be
            # auto-reinitialized at next login. Surface as unavailable.
            raise BackgroundUnavailable(user_id) from e

    def _mint_v2_keys(
        self, user_id: int
    ) -> tuple[bytes, bytes, bytes]:
        """Generate a fresh v2 DEK for `user_id`, persist the wrap, and bump
        the row's enc_version. Returns (dek, salt_dek, wrapped_dek_v2). Used
        by the legacy auto-upgrade path in verify() and by the
        machine-secret-mismatch recovery."""
        salt_dek = _enc.generate_salt()
        dek = _enc.generate_dek()
        kek = _enc.derive_kek(self._machine_secret, salt_dek)
        wrapped_dek_v2 = _enc.wrap_dek(kek, dek)
        with self._lock:
            self._conn.execute(
                "UPDATE users SET salt_dek = ?, wrapped_dek_v2 = ?, "
                "salt_kek = NULL, wrapped_dek = NULL, "
                f"enc_version = {_ENC_VERSION_CURRENT} "
                "WHERE id = ?",
                (salt_dek, wrapped_dek_v2, user_id),
            )
            self._conn.commit()
        return dek, salt_dek, wrapped_dek_v2

    def lookup(self, user_id: int) -> UserRecord | None:
        # Soft-deleted accounts are treated as gone here: a session cookie
        # that outlived a deletion resolves to None and the caller re-routes
        # to login (where recovery is offered).
        with self._lock:
            row = self._conn.execute(
                "SELECT id, username, api_access, email, messaging_contact, "
                "first_name, last_name, tz, date_format, time_format "
                "FROM users WHERE id = ? AND deleted_at IS NULL",
                (user_id,),
            ).fetchone()
        if row is None:
            return None
        return UserRecord(
            id=row[0], username=row[1],
            api_access=bool(row[2]), email=row[3], messaging_contact=row[4],
            first_name=row[5], last_name=row[6], tz=row[7],
            date_format=row[8], time_format=row[9],
        )

    def lookup_by_username(self, username: str) -> UserRecord | None:
        """Resolve an active account by its (case-insensitive) username. Used by
        features that key off a user-typed name — e.g. picking a topic-share
        recipient. Soft-deleted accounts return None, same as lookup()."""
        username = (username or "").strip()
        if not username:
            return None
        with self._lock:
            row = self._conn.execute(
                "SELECT id, username, api_access, email, messaging_contact, "
                "first_name, last_name, tz, date_format, time_format "
                "FROM users WHERE username = ? AND deleted_at IS NULL",
                (username,),
            ).fetchone()
        if row is None:
            return None
        return UserRecord(
            id=row[0], username=row[1],
            api_access=bool(row[2]), email=row[3], messaging_contact=row[4],
            first_name=row[5], last_name=row[6], tz=row[7],
            date_format=row[8], time_format=row[9],
        )

    def set_messaging_contact(self, user_id: int, contact: str | None) -> bool:
        """Connect (or, with None, clear) the account's outbound-messaging
        destination — see aime.messaging and UserRecord.messaging_contact.
        Returns False if there's no matching active account."""
        normalized = (contact or "").strip() or None
        with self._lock:
            cur = self._conn.execute(
                "UPDATE users SET messaging_contact = ? "
                "WHERE id = ? AND deleted_at IS NULL",
                (normalized, user_id),
            )
            self._conn.commit()
        return cur.rowcount > 0

    def set_timezone(self, user_id: int, tz: str | None) -> bool:
        """Persist the account's last-seen IANA timezone (see UserRecord.tz).
        Called from the /send handler when the browser-reported zone changes, so
        it self-corrects as the user travels. A blank/None value clears it.
        Returns False if there's no matching active account."""
        normalized = (tz or "").strip() or None
        with self._lock:
            cur = self._conn.execute(
                "UPDATE users SET tz = ? "
                "WHERE id = ? AND deleted_at IS NULL",
                (normalized, user_id),
            )
            self._conn.commit()
        return cur.rowcount > 0

    def set_date_prefs(
        self, user_id: int, date_format: str | None, time_format: str | None
    ) -> bool:
        """Persist the account's display preferences for dates/times (see
        UserRecord.date_format / .time_format). Called from /send when the
        browser-reported values change. Blank/None clears a field. Returns False
        if there's no matching active account."""
        df = (date_format or "").strip() or None
        tf = (time_format or "").strip() or None
        with self._lock:
            cur = self._conn.execute(
                "UPDATE users SET date_format = ?, time_format = ? "
                "WHERE id = ? AND deleted_at IS NULL",
                (df, tf, user_id),
            )
            self._conn.commit()
        return cur.rowcount > 0

    def set_display_name(
        self, user_id: int,
        first_name: str | None, last_name: str | None,
    ) -> bool:
        """Set (or, with blanks, clear) the account's display-only first/last
        name — see UserRecord. The username is intentionally not touchable here;
        it's the immutable identity. Returns False if there's no matching active
        account. Raises InvalidName if a value is over the length cap."""
        first_name = self._validate_name(first_name)
        last_name = self._validate_name(last_name)
        with self._lock:
            cur = self._conn.execute(
                "UPDATE users SET first_name = ?, last_name = ? "
                "WHERE id = ? AND deleted_at IS NULL",
                (first_name, last_name, user_id),
            )
            self._conn.commit()
        return cur.rowcount > 0

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
    def _validate_password(password: str, *, username: str | None = None) -> None:
        if not isinstance(password, str) or len(password) < _MIN_PASSWORD_LEN:
            raise WeakPassword(
                f"password must be at least {_MIN_PASSWORD_LEN} characters"
            )
        if len(password) > _MAX_PASSWORD_LEN:
            raise WeakPassword(
                f"password must be at most {_MAX_PASSWORD_LEN} characters"
            )
        # Reject the single most-guessed credential: the password equal to the
        # username. Cheap context-specific check (NIST 800-63B 5.1.1.2).
        if username and password.strip().casefold() == username.strip().casefold():
            raise WeakPassword("password must not be the same as your username")

    @staticmethod
    def _validate_name(name: str | None) -> str | None:
        """Normalize an optional display name: trim whitespace, treat blank as
        "not given" (None), and enforce a length cap. Returns the cleaned value.
        Names are free-form, so the only rejection is over-length."""
        if name is None:
            return None
        cleaned = name.strip()
        if not cleaned:
            return None
        if len(cleaned) > _MAX_NAME_LEN:
            raise InvalidName(f"name must be at most {_MAX_NAME_LEN} characters")
        return cleaned

    @staticmethod
    def _validate_email(email: str) -> None:
        if not isinstance(email, str):
            raise InvalidEmail("please enter your email address")
        # Loose practical check — bare-bones "looks like an email". Real
        # validation comes from the user receiving the code we mail to it.
        if "@" not in email or "." not in email.split("@", 1)[-1] or " " in email:
            raise InvalidEmail("that doesn't look like an email address")
        if len(email) > 254:
            raise InvalidEmail("that email address is too long")

    # ---- Email verification (signup 2FA + add-email + login 2FA) ----------
    #
    # Three flows live in the email_verifications table:
    #   * 'signup' — username/password are *held*, not yet a real account,
    #     until the user proves they own the email. complete_signup_verification
    #     promotes the row into a real users row.
    #   * 'add_email' — an existing logged-in user attaches an email to their
    #     account; complete_add_email_verification writes users.email.
    #   * 'login' — a correct password has been seen for an account that
    #     already has an email; a code is mailed to that address and must be
    #     entered before the session is granted. complete_login_verification
    #     just consumes the row (the account is unchanged).
    #
    # The raw 6-digit code is mailed to the user; only its sha256 is stored.
    # The `token` is a fresh 256-bit secret kept in the user's signed session
    # cookie — it identifies which pending row the request is acting on,
    # without re-trusting client-supplied user_id/username/email.

    _VERIFICATION_TTL_SECONDS = 10 * 60
    _VERIFICATION_MAX_ATTEMPTS = 5

    # How long a "remember this device" token stays valid. After this the
    # device has to pass email 2FA again (and can re-trust). 30 days balances
    # convenience against bounding how long a stolen cookie stays useful.
    _TRUSTED_DEVICE_TTL_SECONDS = 30 * 24 * 60 * 60

    # Cap on simultaneously-trusted devices per account — minting a new one past
    # this evicts the oldest. Bounds both the table size and how many live
    # bypass tokens a password-holder can stockpile.
    _MAX_TRUSTED_DEVICES = 10

    @staticmethod
    def _generate_code() -> str:
        # secrets.randbelow gives unbiased 6-digit codes including leading
        # zeros. 10^6 = 1M space; brute force is bounded by max-attempts.
        return f"{secrets.randbelow(1_000_000):06d}"

    @staticmethod
    def _hash_code(code: str) -> str:
        return hashlib.sha256(code.strip().encode("utf-8")).hexdigest()

    def _purge_expired_verifications(self) -> None:
        now = int(time.time())
        self._conn.execute(
            "DELETE FROM email_verifications WHERE expires_at < ?",
            (now,),
        )

    def email_in_use(self, email: str) -> bool:
        """True if some active account already has this email. Case-insensitive."""
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM users "
                "WHERE LOWER(email) = LOWER(?) AND deleted_at IS NULL",
                (email.strip(),),
            ).fetchone()
        return row is not None

    def start_signup_verification(
        self, username: str, password: str, email: str, api_access: bool = True,
        *, first_name: str | None = None, last_name: str | None = None,
    ) -> tuple[str, str, str]:
        """Begin a signup. Validates everything, stashes a pending row, and
        returns (token, code, normalized_email). The caller mails the code;
        the token goes in the user's signed session.

        Raises UsernameTaken, WeakPassword, InvalidUsername, InvalidEmail at
        the same points create() would, so the UI can surface the same errors
        on the signup form before we ever send mail. The optional
        first_name/last_name are held with the pending row and written onto the
        account when the code is confirmed.
        """
        self._validate_username(username)
        self._validate_password(password, username=username)
        self._validate_email(email)
        first_name = self._validate_name(first_name)
        last_name = self._validate_name(last_name)
        email_norm = email.strip()

        # Pre-hash the password so the plaintext never sits in the DB.
        pw_hash = self._hasher.hash(password)
        token = secrets.token_urlsafe(32)
        code = self._generate_code()
        code_hash = self._hash_code(code)
        now = int(time.time())
        expires_at = now + self._VERIFICATION_TTL_SECONDS

        with self._lock:
            # Block obvious duplicates up front so a UsernameTaken doesn't
            # surface only after the user retrieves the code from email.
            taken = self._conn.execute(
                "SELECT 1 FROM users WHERE username = ? AND deleted_at IS NULL",
                (username,),
            ).fetchone()
            if taken is not None:
                raise UsernameTaken(f"username already taken: {username!r}")
            # Inlined email-uniqueness check — calling email_in_use() here
            # would re-enter the (non-reentrant) lock and deadlock.
            dup_email = self._conn.execute(
                "SELECT 1 FROM users "
                "WHERE LOWER(email) = LOWER(?) AND deleted_at IS NULL",
                (email_norm,),
            ).fetchone()
            if dup_email is not None:
                raise InvalidEmail("an account with that email already exists")
            self._purge_expired_verifications()
            self._conn.execute(
                "INSERT INTO email_verifications "
                "(token, purpose, username, password_hash, email, code_hash, "
                "api_access, created_at, expires_at, first_name, last_name) "
                "VALUES (?, 'signup', ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (token, username, pw_hash, email_norm, code_hash,
                 1 if api_access else 0, now, expires_at,
                 first_name, last_name),
            )
            self._conn.commit()
        return token, code, email_norm

    def start_add_email_verification(
        self, user_id: int, email: str
    ) -> tuple[str, str, str]:
        """Begin attaching an email to an existing account. Same return shape
        as start_signup_verification."""
        self._validate_email(email)
        email_norm = email.strip()
        token = secrets.token_urlsafe(32)
        code = self._generate_code()
        code_hash = self._hash_code(code)
        now = int(time.time())
        expires_at = now + self._VERIFICATION_TTL_SECONDS
        with self._lock:
            dup_email = self._conn.execute(
                "SELECT 1 FROM users "
                "WHERE LOWER(email) = LOWER(?) AND deleted_at IS NULL",
                (email_norm,),
            ).fetchone()
            if dup_email is not None:
                raise InvalidEmail("an account with that email already exists")
            self._purge_expired_verifications()
            self._conn.execute(
                "INSERT INTO email_verifications "
                "(token, purpose, user_id, email, code_hash, "
                "created_at, expires_at) "
                "VALUES (?, 'add_email', ?, ?, ?, ?, ?)",
                (token, user_id, email_norm, code_hash, now, expires_at),
            )
            self._conn.commit()
        return token, code, email_norm

    def start_login_verification(
        self, user_id: int, email: str
    ) -> tuple[str, str, str]:
        """Begin a login-time 2FA challenge for an account that already has a
        verified email on file. Mails a fresh 6-digit code to that address and
        stashes a pending 'login' row; same return shape as the other start_*
        methods.

        Unlike signup/add_email there is no uniqueness check — the address is
        the account's own, already stored on users.email. Caller is expected to
        only reach here *after* a successful password verify, so a code is
        never mailed on a bad guess.
        """
        self._validate_email(email)
        email_norm = email.strip()
        token = secrets.token_urlsafe(32)
        code = self._generate_code()
        code_hash = self._hash_code(code)
        now = int(time.time())
        expires_at = now + self._VERIFICATION_TTL_SECONDS
        with self._lock:
            self._purge_expired_verifications()
            self._conn.execute(
                "INSERT INTO email_verifications "
                "(token, purpose, user_id, email, code_hash, "
                "created_at, expires_at) "
                "VALUES (?, 'login', ?, ?, ?, ?, ?)",
                (token, user_id, email_norm, code_hash, now, expires_at),
            )
            self._conn.commit()
        return token, code, email_norm

    def complete_login_verification(self, token: str, code: str) -> int:
        """Finalize a 'login' challenge: validate the code and return the
        user_id the pending row was bound to. Raises VerificationError on a
        wrong / expired / used-up code. Mutates nothing beyond consuming the
        pending row — the account already exists and is unchanged, so the
        caller resolves the DEK and session the same way the password-only
        path does."""
        row = self._consume_verification(token, code, expected_purpose="login")
        return row[2]  # user_id

    def start_password_reset(
        self, identifier: str
    ) -> tuple[str, str, str] | None:
        """Begin a password reset for the account matching `identifier` (a
        username or an email). Mints a 'reset' verification bound to that
        account and returns (token, code, email) for the caller to mail.

        Returns None when no eligible account matches — an account is eligible
        only if it is active and has an email on file (the code can only be
        delivered to the address already stored, never to a caller-supplied
        one). The caller must behave identically whether this returns a tuple
        or None, so the response never reveals whether an account exists.
        """
        ident = (identifier or "").strip()
        if not ident:
            return None
        token = secrets.token_urlsafe(32)
        code = self._generate_code()
        code_hash = self._hash_code(code)
        now = int(time.time())
        expires_at = now + self._VERIFICATION_TTL_SECONDS
        with self._lock:
            # username is COLLATE NOCASE; match email case-insensitively too.
            row = self._conn.execute(
                "SELECT id, username, email FROM users "
                "WHERE deleted_at IS NULL AND email IS NOT NULL AND email != '' "
                "AND (username = ? OR LOWER(email) = LOWER(?)) LIMIT 1",
                (ident, ident),
            ).fetchone()
            if row is None:
                return None
            user_id, username, email = row
            self._purge_expired_verifications()
            # Only one outstanding reset per account: drop any prior 'reset'
            # rows for this user before inserting the new one. The reset code is
            # the *single* factor protecting the account (no password is
            # required), so without this an attacker could open many parallel
            # reset sessions for one victim, each with its own fresh code and
            # 5-guess budget, and amplify a brute force against the 6-digit
            # space. Capping to a single live code bounds the guessable surface
            # to 5 attempts at any instant, refreshed only by re-mailing the
            # victim (which is itself rate-limited and noisy).
            self._conn.execute(
                "DELETE FROM email_verifications "
                "WHERE purpose = 'reset' AND user_id = ?",
                (user_id,),
            )
            self._conn.execute(
                "INSERT INTO email_verifications "
                "(token, purpose, user_id, username, email, code_hash, "
                "created_at, expires_at) "
                "VALUES (?, 'reset', ?, ?, ?, ?, ?, ?)",
                (token, user_id, username, email, code_hash, now, expires_at),
            )
            self._conn.commit()
        return token, code, email

    def complete_password_reset(
        self, token: str, code: str, new_password: str
    ) -> int:
        """Finalize a 'reset': validate the code, then set the account's new
        password. Returns the user_id.

        The new password is validated *before* the one-shot code is consumed,
        so a weak password doesn't burn the verification — the user can correct
        it and resubmit the same code. On success the account's failed-login
        lockout is cleared (proving control of the email is enough to get back
        in) and every trusted-device token is the caller's to revoke. Because
        the at-rest DEK is wrapped under the machine secret (not the password),
        changing the password leaves all encrypted data readable — no re-key.

        Raises WeakPassword for an unacceptable new password and
        VerificationError for a wrong / expired / used-up code.
        """
        # Peek the pending row (without consuming) to get the username for the
        # context-aware password check.
        with self._lock:
            row = self._conn.execute(
                "SELECT username, purpose, expires_at "
                "FROM email_verifications WHERE token = ?",
                (token,),
            ).fetchone()
        if row is None or row[1] != "reset" or row[2] < int(time.time()):
            raise VerificationError(
                "this reset has expired — please start over"
            )
        username = row[0]
        # May raise WeakPassword; the verification row is still intact.
        self._validate_password(new_password, username=username)
        # Validates the code and consumes the row (or raises VerificationError).
        consumed = self._consume_verification(
            token, code, expected_purpose="reset"
        )
        user_id = consumed[2]
        pw_hash = self._hasher.hash(new_password)
        with self._lock:
            self._conn.execute(
                "UPDATE users SET password_hash = ? "
                "WHERE id = ? AND deleted_at IS NULL",
                (pw_hash, user_id),
            )
            self._conn.commit()
        # Lift any standing lockout so the user can sign in with the new
        # password immediately.
        self._clear_failures(username)
        self.log_event(
            EVENT_PASSWORD_RESET, username=username, detail="completed",
        )
        return user_id

    def resend_verification_code(self, token: str) -> tuple[str, str] | None:
        """Rotate the code on an existing pending row and reset its expiry.
        Returns (code, email) for the caller to remail, or None if the token
        is unknown."""
        now = int(time.time())
        code = self._generate_code()
        code_hash = self._hash_code(code)
        expires_at = now + self._VERIFICATION_TTL_SECONDS
        with self._lock:
            row = self._conn.execute(
                "SELECT email FROM email_verifications WHERE token = ?",
                (token,),
            ).fetchone()
            if row is None:
                return None
            self._conn.execute(
                "UPDATE email_verifications "
                "SET code_hash = ?, expires_at = ?, attempts = 0 "
                "WHERE token = ?",
                (code_hash, expires_at, token),
            )
            self._conn.commit()
        return code, row[0]

    def cancel_verification(self, token: str) -> None:
        with self._lock:
            self._conn.execute(
                "DELETE FROM email_verifications WHERE token = ?", (token,)
            )
            self._conn.commit()

    # ---- Trusted devices ("remember this device") -------------------------
    #
    # A device that has just passed login 2FA can be remembered so it skips the
    # emailed code next time. The flow mirrors access keys: we mint a 256-bit
    # token, hand the raw value to the caller (it goes in a long-lived signed
    # cookie), and store only its sha256. A later login presents the cookie;
    # is_trusted_device() confirms the hash maps to a live, unexpired row for
    # that same user before the 2FA step is skipped.

    def _purge_expired_trusted_devices(self) -> None:
        self._conn.execute(
            "DELETE FROM trusted_devices WHERE expires_at < ?",
            (int(time.time()),),
        )

    def create_trusted_device(
        self, user_id: int, *, user_agent: str | None = None
    ) -> tuple[str, int]:
        """Mint a trusted-device token for `user_id`. Returns (raw_token,
        expires_at); the caller stores the raw token in the device cookie and
        only the hash ever touches the DB."""
        token = secrets.token_urlsafe(32)
        token_hash = self._hash_code(token)
        now = int(time.time())
        expires_at = now + self._TRUSTED_DEVICE_TTL_SECONDS
        ua = (user_agent or "").strip()[:256] or None
        with self._lock:
            self._purge_expired_trusted_devices()
            self._conn.execute(
                "INSERT OR REPLACE INTO trusted_devices "
                "(token_hash, user_id, created_at, expires_at, last_used_at, "
                "user_agent) VALUES (?, ?, ?, ?, ?, ?)",
                (token_hash, user_id, now, expires_at, now, ua),
            )
            # Bound how many live tokens one account can accumulate: keep the
            # newest _MAX_TRUSTED_DEVICES, evict the rest. Stops an attacker who
            # has the password from minting an unbounded pile of bypass tokens,
            # and keeps the table small.
            # Order by rowid (monotonic insertion order) so the just-inserted
            # token reliably counts as "newest" even when several are minted
            # within the same wall-clock second (created_at is second-granular).
            self._conn.execute(
                "DELETE FROM trusted_devices WHERE user_id = ? AND token_hash "
                "NOT IN (SELECT token_hash FROM trusted_devices WHERE user_id = ? "
                "ORDER BY rowid DESC LIMIT ?)",
                (user_id, user_id, self._MAX_TRUSTED_DEVICES),
            )
            self._conn.commit()
        return token, expires_at

    def is_trusted_device(self, user_id: int, token: str) -> bool:
        """True if `token` is a live trusted-device token for `user_id`. On a
        hit the row's last_used_at is refreshed (for audit/visibility; the
        expiry is fixed at mint time and not extended). Unknown, mismatched, or
        expired tokens return False without leaking which."""
        if not token:
            return False
        token_hash = self._hash_code(token)
        now = int(time.time())
        with self._lock:
            row = self._conn.execute(
                "SELECT user_id, expires_at FROM trusted_devices "
                "WHERE token_hash = ?",
                (token_hash,),
            ).fetchone()
            if row is None:
                return False
            row_user_id, expires_at = row
            if row_user_id != user_id or expires_at < now:
                # Mismatched owner or expired — never reusable. Drop expired
                # rows opportunistically; leave a mismatch alone (it's a live
                # token for some other account).
                if expires_at < now:
                    self._conn.execute(
                        "DELETE FROM trusted_devices WHERE token_hash = ?",
                        (token_hash,),
                    )
                    self._conn.commit()
                return False
            self._conn.execute(
                "UPDATE trusted_devices SET last_used_at = ? "
                "WHERE token_hash = ?",
                (now, token_hash),
            )
            self._conn.commit()
        return True

    def revoke_trusted_device(self, token: str) -> None:
        """Forget a single trusted device (e.g. on explicit sign-out of this
        device). No-op if the token is unknown."""
        if not token:
            return
        with self._lock:
            self._conn.execute(
                "DELETE FROM trusted_devices WHERE token_hash = ?",
                (self._hash_code(token),),
            )
            self._conn.commit()

    def revoke_all_trusted_devices(self, user_id: int) -> int:
        """Forget every trusted device for an account (e.g. on password change
        or a "sign out everywhere" action). Returns the number removed."""
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM trusted_devices WHERE user_id = ?", (user_id,)
            )
            self._conn.commit()
        return cur.rowcount

    def _consume_verification(
        self, token: str, code: str, expected_purpose: str
    ) -> tuple:
        """Validate the (token, code) pair and atomically delete the row.
        Returns the row tuple on success; raises VerificationError otherwise.

        The row is *only* deleted when the code is correct, so a wrong guess
        consumes an attempt but lets the user try again. After the max number
        of wrong attempts the row is deleted and the user has to start over.
        """
        code_hash = self._hash_code(code)
        now = int(time.time())
        with self._lock:
            self._purge_expired_verifications()
            row = self._conn.execute(
                "SELECT token, purpose, user_id, username, password_hash, "
                "email, code_hash, api_access, attempts, expires_at, "
                "first_name, last_name "
                "FROM email_verifications WHERE token = ?",
                (token,),
            ).fetchone()
            if row is None:
                raise VerificationError(
                    "this verification has expired — please start over"
                )
            (_t, purpose, _uid, _user, _pwh, _email, stored_hash,
             _api, attempts, expires_at, _fn, _ln) = row
            if purpose != expected_purpose or expires_at < now:
                self._conn.execute(
                    "DELETE FROM email_verifications WHERE token = ?", (token,)
                )
                self._conn.commit()
                raise VerificationError(
                    "this verification has expired — please start over"
                )
            # secrets.compare_digest avoids a timing oracle on the stored hash.
            if not secrets.compare_digest(stored_hash, code_hash):
                new_attempts = attempts + 1
                if new_attempts >= self._VERIFICATION_MAX_ATTEMPTS:
                    self._conn.execute(
                        "DELETE FROM email_verifications WHERE token = ?",
                        (token,),
                    )
                    self._conn.commit()
                    raise VerificationError(
                        "too many wrong codes — please start over"
                    )
                self._conn.execute(
                    "UPDATE email_verifications SET attempts = ? "
                    "WHERE token = ?",
                    (new_attempts, token),
                )
                self._conn.commit()
                raise VerificationError("that code isn't right — try again")
            # Correct — consume the row and hand its data back to the caller.
            self._conn.execute(
                "DELETE FROM email_verifications WHERE token = ?", (token,)
            )
            self._conn.commit()
        return row

    def complete_signup_verification(
        self, token: str, code: str
    ) -> tuple[UserRecord, bytes]:
        """Finalize a 'signup' verification: validate the code, then create
        the real users row with the held password hash and email. Returns
        (UserRecord, dek) identical to create()."""
        row = self._consume_verification(token, code, expected_purpose="signup")
        (_t, _purpose, _uid, username, pw_hash, email, _code_hash,
         api_access, _attempts, _exp, first_name, last_name) = row

        # Re-check uniqueness right before the INSERT — a different signup may
        # have completed for the same username in the interval the code was
        # outstanding.
        salt_dek = _enc.generate_salt()
        dek = _enc.generate_dek()
        kek = _enc.derive_kek(self._machine_secret, salt_dek)
        wrapped = _enc.wrap_dek(kek, dek)
        with self._lock:
            try:
                cur = self._conn.execute(
                    "INSERT INTO users "
                    "(username, password_hash, email, salt_dek, wrapped_dek_v2, "
                    "enc_version, api_access, first_name, last_name) "
                    f"VALUES (?, ?, ?, ?, ?, {_ENC_VERSION_CURRENT}, ?, ?, ?)",
                    (username, pw_hash, email, salt_dek, wrapped,
                     int(api_access), first_name, last_name),
                )
                self._conn.commit()
            except sqlite3.IntegrityError as e:
                raise UsernameTaken(
                    f"username already taken: {username!r}"
                ) from e
        return (
            UserRecord(
                id=cur.lastrowid, username=username,
                api_access=bool(api_access), email=email,
                first_name=first_name, last_name=last_name,
            ),
            dek,
        )

    def complete_add_email_verification(
        self, token: str, code: str
    ) -> UserRecord:
        """Finalize an 'add_email' verification: validate the code, then write
        the new email onto the existing user row. Returns the updated record."""
        row = self._consume_verification(token, code, expected_purpose="add_email")
        (_t, _purpose, user_id, _username, _pwh, email, _code_hash,
         _api, _attempts, _exp, _fn, _ln) = row
        with self._lock:
            self._conn.execute(
                "UPDATE users SET email = ? WHERE id = ?",
                (email, user_id),
            )
            self._conn.commit()
        record = self.lookup(user_id)
        if record is None:
            # Should be impossible: the row existed when verification began,
            # and add_email never touches deleted_at. But surface it cleanly
            # rather than crashing if the account was deleted in the meantime.
            raise VerificationError("this account no longer exists")
        return record

    def verification_email(self, token: str) -> str | None:
        """The email a pending verification will deliver to, for display
        on the code-entry page (e.g. "we sent a code to a@b.com")."""
        with self._lock:
            row = self._conn.execute(
                "SELECT email, expires_at FROM email_verifications "
                "WHERE token = ?",
                (token,),
            ).fetchone()
        if row is None or row[1] < int(time.time()):
            return None
        return row[0]

    # ---- Lockout bookkeeping ---------------------------------------------

    def _raise_if_locked(self, username: str, *, ip: str | None = None) -> None:
        now = int(time.time())
        with self._lock:
            row = self._conn.execute(
                "SELECT locked_until FROM auth_attempts WHERE username = ?",
                (username,),
            ).fetchone()
        if row and row[0] and row[0] > now:
            self.log_event(
                EVENT_LOGIN_WHILE_LOCKED, username=username, ip=ip,
                detail=f"{row[0] - now}s remaining",
            )
            raise AccountLocked(seconds_remaining=row[0] - now)

    def _register_failure(
        self, username: str, *, ip: str | None = None,
        unknown_user: bool = False,
    ) -> None:
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
            newly_locked = fail_count >= _FAIL_THRESHOLD and not (
                row and row[0] >= _FAIL_THRESHOLD
            )
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
        # Audit-log the failure outside the lock to keep the critical section
        # short. The log_event call takes its own lock.
        self.log_event(
            EVENT_LOGIN_UNKNOWN_USER if unknown_user else EVENT_LOGIN_BAD_PASSWORD,
            username=username, ip=ip,
            detail=f"fail_count={fail_count}",
        )
        if newly_locked:
            self.log_event(
                EVENT_LOCKOUT_STARTED, username=username, ip=ip,
                detail=f"locked {_LOCK_SECONDS}s after {fail_count} failures",
            )

    def _clear_failures(self, username: str) -> None:
        with self._lock:
            self._conn.execute(
                "DELETE FROM auth_attempts WHERE username = ?", (username,)
            )
            self._conn.commit()

    # ---- Auth-event audit log --------------------------------------------

    def log_event(
        self, kind: str, *,
        username: str | None = None,
        ip: str | None = None,
        detail: str | None = None,
    ) -> None:
        """Append one row to auth_events. Caller-facing entry point so the web
        frontend can log non-verify failures (signup throttles, signup form
        rejections) at the layer that knows the source IP. Prunes rows older
        than _AUTH_EVENT_TTL on roughly one in 64 writes — cheap amortized
        bound on table size."""
        now = int(time.time())
        with self._lock:
            self._conn.execute(
                "INSERT INTO auth_events (ts, kind, username, ip, detail) "
                "VALUES (?, ?, ?, ?, ?)",
                (now, kind, username, ip, detail),
            )
            if now & 0x3F == 0:
                self._conn.execute(
                    "DELETE FROM auth_events WHERE ts < ?",
                    (now - _AUTH_EVENT_TTL,),
                )
            self._conn.commit()

    def recent_auth_events(
        self, limit: int = 200, *, since_ts: int | None = None,
        kinds: tuple[str, ...] | None = None,
    ) -> list[dict]:
        """Newest first. `since_ts` and `kinds` are optional filters. Returns
        plain dicts so the dashboard can pass them straight to the template."""
        q = "SELECT id, ts, kind, username, ip, detail FROM auth_events"
        clauses, params = [], []
        if since_ts is not None:
            clauses.append("ts >= ?")
            params.append(since_ts)
        if kinds:
            clauses.append("kind IN (" + ",".join("?" * len(kinds)) + ")")
            params.extend(kinds)
        if clauses:
            q += " WHERE " + " AND ".join(clauses)
        q += " ORDER BY ts DESC, id DESC LIMIT ?"
        params.append(int(limit))
        with self._lock:
            rows = self._conn.execute(q, params).fetchall()
        return [
            {"id": r[0], "ts": r[1], "kind": r[2],
             "username": r[3], "ip": r[4], "detail": r[5]}
            for r in rows
        ]

    def auth_event_summary(self, window_seconds: int) -> dict[str, int]:
        """Counts by kind over the last `window_seconds`. Drives the
        dashboard's at-a-glance counters. Unknown kinds are reported as 0 so
        the template can render a stable row order."""
        cutoff = int(time.time()) - window_seconds
        with self._lock:
            rows = self._conn.execute(
                "SELECT kind, COUNT(*) FROM auth_events WHERE ts >= ? "
                "GROUP BY kind",
                (cutoff,),
            ).fetchall()
        counts = {k: 0 for k in (
            EVENT_LOGIN_UNKNOWN_USER, EVENT_LOGIN_BAD_PASSWORD,
            EVENT_LOGIN_WHILE_LOCKED, EVENT_LOCKOUT_STARTED,
            EVENT_SIGNUP_RATE_LIMITED, EVENT_SIGNUP_FAILED,
            EVENT_LOGIN_IP_THROTTLED, EVENT_PASSWORD_RESET,
        )}
        for kind, n in rows:
            counts[kind] = n
        return counts

    def distinct_event_ips(self, window_seconds: int, limit: int = 25) -> list[dict]:
        """Top IPs by failure volume in the last `window_seconds`. Useful for
        spotting a single host hammering the login. Excludes NULL IPs (those
        come from background callers, not network requests)."""
        cutoff = int(time.time()) - window_seconds
        with self._lock:
            rows = self._conn.execute(
                "SELECT ip, COUNT(*) AS n, MAX(ts) AS last_ts "
                "FROM auth_events WHERE ts >= ? AND ip IS NOT NULL "
                "GROUP BY ip ORDER BY n DESC, last_ts DESC LIMIT ?",
                (cutoff, int(limit)),
            ).fetchall()
        return [{"ip": r[0], "count": r[1], "last_ts": r[2]} for r in rows]


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
                # Drop the bucket if it only held now-expired hits, so the dict
                # can't grow without bound as distinct keys (IPs) churn.
                if not q:
                    self._hits.pop(key, None)
                return False
            q.append(now)
            return True

    def blocked(self, key: str) -> bool:
        """True if `key` is currently at/over the limit, *without* recording a
        new hit. Lets a caller reject early (e.g. before an expensive password
        hash) when a key has already tripped the limit via prior failures."""
        now = time.monotonic()
        cutoff = now - self._window
        with self._lock:
            q = self._hits.get(key)
            if q is None:
                return False
            while q and q[0] < cutoff:
                q.popleft()
            if not q:
                # Fully expired — reclaim the bucket and report not-blocked.
                self._hits.pop(key, None)
                return False
            return len(q) >= self._limit


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
