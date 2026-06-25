"""Server-side error/diagnostics capture store.

Where :mod:`aime.feedback` records messages a *user* deliberately sends us, this
records the errors *Aime itself* hits — a transient Anthropic outage, a malformed
request, an unexpected exception in a turn — so the admin dashboard's Errors tab
has something concrete to inspect instead of a console line nobody saw.

Each captured error keeps the bits that actually help diagnose it: the exception
class, the HTTP ``status_code`` and Anthropic ``request_id`` (to correlate with
provider support), the model, the user/session it happened to, and a clamped
traceback. A short ``reference`` is handed back to the chat UI so a user can quote
it when reporting — and so that report lines up with this row.

To keep the table readable during an outage (when the same error can fire on
every turn for minutes), :meth:`ErrorStore.capture` **deduplicates**: an
identical error — same ``(source, error_class, status_code)`` signature — seen
again within a short window bumps a ``count`` on the existing row instead of
inserting a new one, reusing its ``reference``.

Cross-user, so — like :mod:`aime.feedback` and :mod:`aime.quota` — it lives as
one file at the database root (beside ``feedback.sql``) rather than inside any
one user's silo. Thread-safe via a single connection under a lock, the same
pattern as :class:`aime.feedback.FeedbackStore`.
"""

from __future__ import annotations

import os
import secrets
import sqlite3
import threading
import traceback as _tb


# Triage lifecycle, mirroring feedback's. ``new`` is the unread default; the tab
# badge counts everything not yet ``resolved``. Order here is dashboard order.
STATUSES = ("new", "seen", "resolved")

# How a captured error is bucketed — both for the user-facing message (see
# `classify`) and for the public health page's at-a-glance breakdown. Ordered
# least-to-most "Aime's fault"; only the last two (``auth``, ``unknown``) point
# at us rather than the provider or the request, so only those move the
# service-health dial (see aime.health). Each means, roughly:
#
#   transient   provider briefly busy / a temporary blip — retry and it passes
#               (5xx incl. 529 overloaded, timeouts, dropped connections).
#   rate_limit  a usage/rate ceiling was hit (429) — a momentary pause, not a
#               fault; eases on its own.
#   input       the *request* couldn't be processed — a too-large or unsupported
#               attachment, a malformed message (400/413/422). Consistent and
#               explainable; the user can adjust and retry. This is where an
#               oversized picture the API rejects lands — NOT ``unknown``.
#   auth        a credential/permission problem reaching the model (401/403).
#               Operator-side and usually total, so it counts as a service fault.
#   unknown     a genuinely unexpected internal error (a bug, an exception we
#               didn't anticipate). The only "something's broken in Aime" bucket.
CATEGORIES = ("transient", "rate_limit", "input", "auth", "unknown")

# Categories an earlier version wrote, mapped to their current name so the
# health page buckets historical rows correctly during/after the rename.
_LEGACY_CATEGORY_ALIASES = {"client": "input"}

# Recurrences of the same signature within this window fold onto one row rather
# than inserting a fresh one. Long enough to collapse an outage burst, short
# enough that a genuinely new flare-up after a quiet spell starts a new row.
_DEDUP_WINDOW = "-1 hour"

# HTTP statuses per bucket. Transient: request timeout, conflict, and the 5xx
# family incl. 529 overloaded. Rate limit, input (bad/too-large/unprocessable
# request) and auth (unauthenticated/forbidden) are split out so the breakdown
# tells the user *which* kind of problem they're seeing.
_TRANSIENT_STATUS = frozenset({408, 409, 500, 502, 503, 504, 529})
_RATE_LIMIT_STATUS = frozenset({429})
_INPUT_STATUS = frozenset({400, 413, 422})
_AUTH_STATUS = frozenset({401, 403})

# Defensive caps so one capture can't bloat the store.
_MAX_MESSAGE = 4000
_MAX_TRACEBACK = 8000


# User-facing copy. Calm and specific, per the friendly-error-messaging intent.
# ``None`` means "no better line than the frontend's existing generic one".
_MSG_TRANSIENT = ("Aime's servers are briefly busy — give it a moment and try "
                  "again.")
_MSG_RATE_LIMIT = ("Aime has hit its rate limit for the moment — give it a "
                   "minute and try again.")
_MSG_INPUT = ("Aime couldn't process that — the message or file may be too "
              "large or in a format it can't read.")
_MSG_AUTH = ("Aime is having trouble reaching its service right now. Please "
             "try again shortly.")
_MSG_GENERIC: str | None = None


# Anthropic error types, imported defensively so a missing/renamed SDK degrades
# to "unknown" classification instead of breaking capture entirely.
class _Never(Exception):
    """Sentinel that no real exception is ever an instance of."""


try:  # pragma: no cover - exercised only when the SDK is present
    from anthropic import (
        APIStatusError,
        APIConnectionError,
        APITimeoutError,
        RateLimitError,
        InternalServerError,
        BadRequestError,
        AuthenticationError,
        PermissionDeniedError,
    )
except Exception:  # pragma: no cover - SDK absent or changed shape
    APIStatusError = APIConnectionError = APITimeoutError = _Never
    RateLimitError = InternalServerError = BadRequestError = _Never
    AuthenticationError = PermissionDeniedError = _Never


def _clamp(text: str | None, limit: int) -> str | None:
    if text is None:
        return None
    text = text.strip()
    if not text:
        return None
    return text[:limit]


def _status_of(exc: BaseException) -> int | None:
    """The HTTP status carried by an exception, from the Anthropic SDK's
    ``status_code`` or a Werkzeug ``HTTPException.code`` (so a Flask-side 413
    "request too large" is classified as ``input``, not ``unknown``)."""
    status = getattr(exc, "status_code", None)
    if not isinstance(status, int):
        status = getattr(exc, "code", None)
    return status if isinstance(status, int) else None


def classify(exc: BaseException) -> tuple[str, str | None]:
    """Bucket an exception into a ``(category, user_message)`` pair.

    ``category`` is one of :data:`CATEGORIES`; ``user_message`` is the calm line
    to show the user, or ``None`` to keep the frontend's existing generic one.
    Checked most-specific first: a ``RateLimitError`` (a subclass of
    ``APIStatusError``) is a rate limit, not a generic transient blip, and an
    auth failure is distinct from a plain bad request.
    """
    status = _status_of(exc)
    if isinstance(exc, RateLimitError) or status in _RATE_LIMIT_STATUS:
        return "rate_limit", _MSG_RATE_LIMIT
    if isinstance(exc, (InternalServerError, APIConnectionError,
                        APITimeoutError)) or status in _TRANSIENT_STATUS:
        return "transient", _MSG_TRANSIENT
    if isinstance(exc, (AuthenticationError, PermissionDeniedError)) \
            or status in _AUTH_STATUS:
        return "auth", _MSG_AUTH
    if isinstance(exc, BadRequestError) or status in _INPUT_STATUS:
        return "input", _MSG_INPUT
    return "unknown", _MSG_GENERIC


def _reference() -> str:
    """A short, lowercase, unambiguous public id a user can quote back."""
    return secrets.token_hex(4)  # 8 hex chars, e.g. "a1b2c3d4"


class ErrorStore:
    """SQLite-backed diagnostics store. One file at the database root."""

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
                CREATE TABLE IF NOT EXISTS errors (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    signature   TEXT NOT NULL,
                    category    TEXT NOT NULL DEFAULT 'unknown',
                    source      TEXT,
                    error_class TEXT,
                    status_code INTEGER,
                    request_id  TEXT,
                    model       TEXT,
                    username    TEXT,
                    session_id  TEXT,
                    message     TEXT,
                    traceback   TEXT,
                    reference   TEXT NOT NULL,
                    count       INTEGER NOT NULL DEFAULT 1,
                    status      TEXT NOT NULL DEFAULT 'new',
                    admin_note  TEXT,
                    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
                    last_seen   TEXT NOT NULL DEFAULT (datetime('now'))
                );
                CREATE INDEX IF NOT EXISTS idx_errors_status ON errors(status);
                CREATE INDEX IF NOT EXISTS idx_errors_signature
                    ON errors(signature);
                """
            )
            self._conn.commit()

    # -- write path (capture sink) ------------------------------------------

    def capture(self, exc: BaseException, *, source: str,
                session_id: str | None = None, username: str | None = None,
                model: str | None = None) -> dict:
        """Record an error (or fold it onto a recent identical one) and return
        ``{"reference", "category", "user_message"}`` for the caller to surface.

        Best-effort: any failure to persist is swallowed and still returns a
        usable classification, so capture can never itself break a turn.
        """
        category, user_message = classify(exc)
        error_class = type(exc).__name__
        status_code = _status_of(exc)
        request_id = getattr(exc, "request_id", None)
        message = _clamp(str(exc), _MAX_MESSAGE)
        tb = _clamp(
            "".join(_tb.format_exception(type(exc), exc, exc.__traceback__)),
            _MAX_TRACEBACK,
        )
        signature = f"{source}|{error_class}|{status_code}"
        result = {"reference": None, "category": category,
                  "user_message": user_message}
        try:
            with self._lock:
                row = self._conn.execute(
                    "SELECT id, reference FROM errors "
                    "WHERE signature = ? "
                    f"AND last_seen >= datetime('now', '{_DEDUP_WINDOW}') "
                    "ORDER BY last_seen DESC, id DESC LIMIT 1",
                    (signature,),
                ).fetchone()
                if row is not None:
                    # Recurrence: bump the count and refresh the most-recent
                    # occurrence details, keeping the original reference/status.
                    self._conn.execute(
                        "UPDATE errors SET count = count + 1, "
                        "last_seen = datetime('now'), request_id = ?, "
                        "session_id = ?, username = ?, message = ?, "
                        "traceback = ? WHERE id = ?",
                        (request_id, session_id, username, message, tb,
                         row["id"]),
                    )
                    self._conn.commit()
                    result["reference"] = row["reference"]
                    return result
                reference = _reference()
                self._conn.execute(
                    "INSERT INTO errors (signature, category, source, "
                    "error_class, status_code, request_id, model, username, "
                    "session_id, message, traceback, reference) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (signature, category, source, error_class, status_code,
                     request_id, model, username, session_id, message, tb,
                     reference),
                )
                self._conn.commit()
                result["reference"] = reference
                return result
        except Exception:
            # Never let diagnostics persistence break the path it observes.
            return result

    # -- read path (admin dashboard) ----------------------------------------

    def list(self, status: str | None = None) -> list[dict]:
        """All errors, most-recently-seen first. ``status`` filters to one
        lifecycle state when given and recognised."""
        if status in STATUSES:
            rows = self._conn.execute(
                "SELECT * FROM errors WHERE status = ? "
                "ORDER BY last_seen DESC, id DESC",
                (status,),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM errors ORDER BY last_seen DESC, id DESC"
            ).fetchall()
        return [dict(r) for r in rows]

    def get(self, error_id: int) -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM errors WHERE id = ?", (error_id,)
        ).fetchone()
        return dict(row) if row else None

    def counts(self) -> dict:
        """``{status: n, ..., 'total': n, 'unresolved': n}`` for the tab badge
        and the at-a-glance header."""
        rows = self._conn.execute(
            "SELECT status, COUNT(*) AS n FROM errors GROUP BY status"
        ).fetchall()
        out = {s: 0 for s in STATUSES}
        for r in rows:
            out[r["status"]] = r["n"]
        out["total"] = sum(out[s] for s in STATUSES)
        out["unresolved"] = out["new"] + out["seen"]
        return out

    def recent(self, window_hours: int = 24) -> dict:
        """Aggregate error activity over the last ``window_hours`` for the
        public health page. Sums each row's ``count`` (so an outage burst that
        folded onto one signature is counted in full) bucketed by category,
        alongside the number of distinct signatures and the most recent
        occurrence.

        Returns ``{'events', 'signatures', 'transient', 'client', 'unknown',
        'last_seen'}``. Best-effort: a query failure returns a zeroed summary so
        the health page can never be broken by the very store it reports on.
        """
        out = {"events": 0, "signatures": 0, "last_seen": None}
        for cat in CATEGORIES:
            out[cat] = 0
        try:
            rows = self._conn.execute(
                "SELECT category, COUNT(*) AS sigs, "
                "SUM(count) AS events, MAX(last_seen) AS last_seen "
                "FROM errors WHERE last_seen >= datetime('now', ?) "
                "GROUP BY category",
                (f"-{int(window_hours)} hours",),
            ).fetchall()
        except Exception:
            return out
        for r in rows:
            cat = _LEGACY_CATEGORY_ALIASES.get(r["category"], r["category"])
            if cat not in CATEGORIES:
                cat = "unknown"
            events = r["events"] or 0
            out["events"] += events
            out["signatures"] += r["sigs"] or 0
            out[cat] += events
            if r["last_seen"] and (
                out["last_seen"] is None or r["last_seen"] > out["last_seen"]
            ):
                out["last_seen"] = r["last_seen"]
        return out

    # -- triage (admin dashboard) -------------------------------------------

    def set_status(self, error_id: int, status: str) -> bool:
        """Move an error to a new lifecycle state. False on unknown status or
        missing row."""
        if status not in STATUSES:
            return False
        with self._lock:
            cur = self._conn.execute(
                "UPDATE errors SET status = ? WHERE id = ?",
                (status, error_id),
            )
            self._conn.commit()
            return cur.rowcount > 0

    def set_note(self, error_id: int, note: str | None) -> bool:
        """Attach (or clear, with a blank) the admin triage note. False if the
        row doesn't exist."""
        note = _clamp(note, _MAX_TRACEBACK)
        with self._lock:
            cur = self._conn.execute(
                "UPDATE errors SET admin_note = ? WHERE id = ?",
                (note, error_id),
            )
            self._conn.commit()
            return cur.rowcount > 0
