"""Per-user daily usage budget — the authoritative cost-control ledger.

This is deliberately separate from :mod:`aime.usage`. The usage log is an
*opt-in, best-effort, anonymizable* record for reporting: it can be switched off
(``AIME_USAGE_STATS=0``), it swallows its own write errors, and it nulls the
username when linkage is off. None of that is acceptable for enforcement, so the
budget lives in its own always-on store.

The model is a **token bucket** (see ``docs/usage-limits.md``):

  * Each user has a ``balance`` in USD and a ``last_update`` timestamp.
  * The balance refills **continuously** at the user's tier rate
    (``config.USAGE_TIERS[tier]`` USD/day) — no midnight cliff — and is clamped
    to a ceiling of ``USAGE_BANK_DAYS`` days' allowance. A quiet day's unused
    allowance therefore carries forward to a busy one, up to the ceiling.
  * Every API call's real cost (priced via :mod:`aime.pricing`) is debited. The
    balance may go negative — that is "over budget".
  * A fresh user starts **full** (at the ceiling).

What happens when the balance runs out is **deliberately not decided here**: the
single seam is :func:`enforcement_decision`, which today only distinguishes
"allow", "running low", and "over". Callers currently *notify* on the latter two
and block on neither. Turning ``OVER`` into a hard block (or a forced-cheap
downgrade) later is a change at the call sites, not in this math.

Enforcement is armed by ``AIME_ACCESS_MODE`` (``keys``/``billing``), mirroring
the ``/send`` ``api_access`` gate; in ``open`` mode no meter is attached and this
module is never touched.
"""

from __future__ import annotations

import enum
import os
import sqlite3
import threading
import datetime
from typing import Callable

from . import config


_SECONDS_PER_DAY = 86400.0


class Decision(enum.Enum):
    """Outcome of consulting the budget after a refill (and any debit).

    The *action* attached to each is the caller's choice and is intentionally
    minimal today — see the module docstring.
    """

    ALLOW = "allow"          # comfortably within budget
    NOTIFY_LOW = "notify_low"  # under the low-water mark; surface a gentle nudge
    OVER = "over"            # balance exhausted (<= 0); notify only, for now


def enforcement_decision(
    balance: float,
    daily_cap: float,
    *,
    notify_low_fraction: float | None = None,
) -> Decision:
    """Classify a (refilled) balance. THE enforcement seam.

    ``OVER`` once the balance is spent; ``NOTIFY_LOW`` once it drops below
    ``notify_low_fraction`` of a single day's allowance; else ``ALLOW``. A
    non-positive ``daily_cap`` (misconfiguration) fails open to ``ALLOW`` so a
    bad config never locks everyone out.
    """
    if daily_cap <= 0:
        return Decision.ALLOW
    if balance <= 0:
        return Decision.OVER
    frac = config.USAGE_NOTIFY_LOW_FRACTION if notify_low_fraction is None else notify_low_fraction
    if balance < daily_cap * frac:
        return Decision.NOTIFY_LOW
    return Decision.ALLOW


def make_status(balance: float, daily_cap: float, ceiling: float) -> dict:
    """A display-ready snapshot of a budget. The user never sees dollars — the
    UI renders ``pct_of_day`` (100% = one full day's allowance; reads higher
    when banked) and ``days_banked``. Raw ``balance`` is kept for the admin view.
    """
    if daily_cap > 0:
        pct_of_day = max(0.0, balance) / daily_cap * 100.0
        days_banked = max(0.0, balance) / daily_cap
    else:
        pct_of_day = 0.0
        days_banked = 0.0
    # A stable 0–100% "fullness" gauge: how full the bank is relative to its
    # ceiling. Unlike pct_of_day (which reads up to 700% when banked) this is the
    # intuitive battery-style percentage shown to the user and the admin.
    if ceiling > 0:
        pct_full = min(100.0, max(0.0, balance) / ceiling * 100.0)
    else:
        pct_full = 0.0
    return {
        "balance": round(balance, 6),
        "daily_cap": daily_cap,
        "ceiling": ceiling,
        "pct_of_day": round(pct_of_day, 1),
        "pct_full": round(pct_full, 1),
        "days_banked": round(days_banked, 2),
        "over": balance <= 0,
        "decision": enforcement_decision(balance, daily_cap).value,
    }


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)


def _parse_ts(stamp: str | None) -> datetime.datetime:
    if not stamp:
        return _utcnow()
    try:
        return datetime.datetime.fromisoformat(stamp)
    except ValueError:
        return _utcnow()


def _refill(balance: float, last_update: datetime.datetime,
            daily_cap: float, ceiling: float, now: datetime.datetime) -> float:
    """Token-bucket accrual: add the allowance earned since ``last_update`` and
    clamp to ``ceiling``. Pure — no IO. A clock that appears to run backwards
    (clamped at 0 elapsed) never *removes* balance."""
    elapsed_days = max(0.0, (now - last_update).total_seconds() / _SECONDS_PER_DAY)
    return min(ceiling, balance + elapsed_days * daily_cap)


class QuotaStore:
    """SQLite-backed per-user budget. Thread-safe via a single connection under
    a lock — the same pattern as :class:`aime.auth.LocalAuthBackend` and
    :class:`aime.topic_shares.ShareStore`, and fine for a personal web app's
    concurrency. One file at the database root (beside auth.sql); drop the file
    and this module to remove the feature."""

    def __init__(self, db_path: str):
        self._db_path = db_path
        os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
        self._lock = threading.Lock()
        # check_same_thread=False + explicit lock: shared across Flask threads.
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._init_schema()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def _init_schema(self) -> None:
        with self._lock:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS usage_buckets (
                    username    TEXT NOT NULL PRIMARY KEY,
                    balance     REAL NOT NULL,
                    last_update TEXT NOT NULL DEFAULT (datetime('now'))
                );
                """
            )
            self._conn.commit()

    def _row(self, username: str) -> tuple[float, datetime.datetime] | None:
        r = self._conn.execute(
            "SELECT balance, last_update FROM usage_buckets WHERE username = ?",
            (username,),
        ).fetchone()
        if r is None:
            return None
        return float(r[0]), _parse_ts(r[1])

    def read(self, username: str, daily_cap: float, ceiling: float) -> float:
        """Current refilled balance **without persisting** — for display. A user
        with no row yet reads as full (the ceiling); the row is created lazily on
        the first debit, so a glance never writes."""
        now = _utcnow()
        with self._lock:
            row = self._row(username)
        if row is None:
            return ceiling
        balance, last_update = row
        return _refill(balance, last_update, daily_cap, ceiling, now)

    def debit(self, username: str, daily_cap: float, ceiling: float,
              cost: float) -> float:
        """Refill, subtract ``cost``, persist, and return the new balance (which
        may be negative). Atomic under the store lock. A first-time user is
        seeded full before the debit."""
        now = _utcnow()
        with self._lock:
            row = self._row(username)
            base = ceiling if row is None else _refill(
                row[0], row[1], daily_cap, ceiling, now
            )
            new_balance = base - max(0.0, cost)
            self._conn.execute(
                "INSERT INTO usage_buckets (username, balance, last_update) "
                "VALUES (?, ?, ?) "
                "ON CONFLICT(username) DO UPDATE SET balance = excluded.balance, "
                "last_update = excluded.last_update",
                (username, new_balance, now.isoformat(timespec="seconds")),
            )
            self._conn.commit()
        return new_balance


class QuotaMeter:
    """Per-user handle over a :class:`QuotaStore`, constructed per session like
    :class:`aime.model_router.ModelRouter`. Resolves the user's current daily cap
    through a callable so an admin tier change takes effect without a restart;
    the ceiling is derived from ``config.USAGE_BANK_DAYS``.
    """

    def __init__(self, store: QuotaStore, username: str,
                 daily_cap_resolver: Callable[[], float]):
        self._store = store
        self._username = username
        self._resolve_cap = daily_cap_resolver

    def _cap_and_ceiling(self) -> tuple[float, float]:
        try:
            cap = float(self._resolve_cap())
        except Exception:
            cap = config.tier_daily_cap(config.USAGE_DEFAULT_TIER)
        return cap, cap * config.USAGE_BANK_DAYS

    def debit(self, cost: float) -> Decision:
        """Charge ``cost`` USD against the budget and return the resulting
        :class:`Decision`. Never raises for normal operation."""
        cap, ceiling = self._cap_and_ceiling()
        balance = self._store.debit(self._username, cap, ceiling, cost)
        return enforcement_decision(balance, cap)

    def status(self) -> dict:
        """Display-ready budget snapshot (see :func:`make_status`). Read-only —
        does not persist."""
        cap, ceiling = self._cap_and_ceiling()
        balance = self._store.read(self._username, cap, ceiling)
        return make_status(balance, cap, ceiling)
