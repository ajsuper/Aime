"""Unit tests for the usage-budget token bucket (aime.quota) and the shared
cost model (aime.pricing).

Covers: the daily grant adds one day's allowance per UTC midnight crossed and
clamps at the 7-day ceiling; it wipes overshoot debt at the reset (floors at 0
before granting); there is no intraday change; a backwards clock never removes
balance; make_status carries seconds_to_reset; a debit decrements and may go
negative; a fresh user starts full; the enforcement_decision thresholds; a live
tier change flips the daily rate; the auth `tier` column defaults old rows to
'light'; and that aime.pricing prices a record identically to the legacy
usage_report path.
"""

import datetime
import sqlite3

import pytest

from aime import quota
from aime import pricing
from aime import config


# --- pure token-bucket math -------------------------------------------------

def test_grant_adds_one_day_per_boundary_and_clamps():
    now = datetime.datetime(2026, 6, 16, 12, 0, 0)
    last = now - datetime.timedelta(days=2)  # two UTC midnights crossed
    cap, ceiling = 1.50, 1.50 * 7
    # 2 daily grants of $1.50 banked on top of a $1.00 balance.
    assert quota._grant(1.00, last, cap, ceiling, now) == pytest.approx(4.00)
    # A long gap can never exceed the ceiling.
    old = now - datetime.timedelta(days=365)
    assert quota._grant(0.0, old, cap, ceiling, now) == pytest.approx(ceiling)


def test_grant_wipes_overshoot_debt_at_reset():
    """A balance driven negative by an overshooting turn is floored at 0 before
    the day's allowance is added — so debt never carries past a reset. One day
    crossed lands a debtor on exactly one day's allowance, not (debt + a day)."""
    now = datetime.datetime(2026, 6, 16, 12, 0, 0)
    cap, ceiling = 1.50, 1.50 * 7
    one_day = now - datetime.timedelta(days=1)
    assert quota._grant(-2.0, one_day, cap, ceiling, now) == pytest.approx(1.50)
    # Multiple days: floor bites once, then whole days accrue from a clean base.
    three_days = now - datetime.timedelta(days=3)
    assert quota._grant(-2.0, three_days, cap, ceiling, now) == pytest.approx(4.50)


def test_grant_has_no_intraday_trickle():
    cap, ceiling = 1.50, 1.50 * 7
    # Same calendar day, hours apart: no grant, balance unchanged (no trickle).
    now = datetime.datetime(2026, 6, 16, 12, 0, 0)
    earlier = datetime.datetime(2026, 6, 16, 2, 0, 0)
    assert quota._grant(3.0, earlier, cap, ceiling, now) == pytest.approx(3.0)
    # Crossing midnight grants a whole day even if only a couple hours elapsed.
    before_midnight = datetime.datetime(2026, 6, 15, 23, 0, 0)
    just_after = datetime.datetime(2026, 6, 16, 1, 0, 0)
    assert quota._grant(3.0, before_midnight, cap, ceiling, just_after) \
        == pytest.approx(4.50)


def test_grant_never_removes_balance_on_backwards_clock():
    now = datetime.datetime(2026, 6, 16, 12, 0, 0)
    future = now + datetime.timedelta(days=1)  # last_update "after" now
    assert quota._grant(5.0, future, 1.5, 10.5, now) == pytest.approx(5.0)


def test_make_status_carries_seconds_to_reset():
    # 18:00 UTC -> next reset (00:00) is 6 hours away.
    now = datetime.datetime(2026, 6, 16, 18, 0, 0)
    st = quota.make_status(5.0, 1.50, 10.5, now=now)
    assert st["seconds_to_reset"] == 6 * 3600


def test_enforcement_decision_thresholds():
    cap = 1.50  # notify-low fraction default 0.25 -> threshold $0.375
    assert quota.enforcement_decision(1.00, cap) is quota.Decision.ALLOW
    assert quota.enforcement_decision(0.30, cap) is quota.Decision.NOTIFY_LOW
    assert quota.enforcement_decision(0.0, cap) is quota.Decision.OVER
    assert quota.enforcement_decision(-5.0, cap) is quota.Decision.OVER
    # Misconfigured cap fails open (never locks everyone out).
    assert quota.enforcement_decision(0.0, 0.0) is quota.Decision.ALLOW


# --- store + meter ----------------------------------------------------------

@pytest.fixture
def store(tmp_path):
    return quota.QuotaStore(str(tmp_path / "quota.sql"))


def test_fresh_user_reads_full(store):
    cap, ceiling = 0.75, 0.75 * 7
    # No row yet -> reads as the ceiling, and does NOT create a row.
    assert store.read("alice", cap, ceiling) == pytest.approx(ceiling)
    row = store._conn.execute(
        "SELECT COUNT(*) FROM usage_buckets WHERE username='alice'"
    ).fetchone()
    assert row[0] == 0


def test_debit_decrements_and_persists(store):
    cap, ceiling = 1.50, 1.50 * 7
    bal1 = store.debit("bob", cap, ceiling, 2.0)
    # Started full at the ceiling; first debit subtracts exactly (a fresh user
    # seeds at the ceiling and there is no intraday trickle to add).
    assert bal1 == pytest.approx(ceiling - 2.0, abs=1e-2)
    bal2 = store.debit("bob", cap, ceiling, ceiling)  # overspend
    assert bal2 < 0


def test_meter_status_and_decision(store):
    meter = quota.QuotaMeter(store, "carol", lambda: 1.50)
    st = meter.status()
    assert st["over"] is False
    assert st["days_banked"] == pytest.approx(7.0, abs=0.01)
    assert st["decision"] == "allow"
    # Spend everything -> OVER.
    assert meter.debit(100.0) is quota.Decision.OVER
    assert meter.status()["over"] is True


def test_tier_change_flips_daily_rate(store):
    # The resolver reads a mutable holder, mimicking an admin tier change.
    holder = {"cap": 0.75}
    meter = quota.QuotaMeter(store, "dave", lambda: holder["cap"])
    assert meter.status()["daily_cap"] == 0.75
    assert meter.status()["ceiling"] == pytest.approx(0.75 * 7)
    holder["cap"] = 1.50
    assert meter.status()["daily_cap"] == 1.50
    assert meter.status()["ceiling"] == pytest.approx(1.50 * 7)


def test_make_status_clamps_negative_for_display():
    st = quota.make_status(-3.0, 1.50, 10.5)
    assert st["balance"] == pytest.approx(-3.0)  # raw kept for admins
    assert st["pct_of_day"] == 0.0               # display never goes negative
    assert st["pct_full"] == 0.0
    assert st["days_banked"] == 0.0
    assert st["over"] is True


def test_make_status_pct_full_is_a_0_to_100_gauge():
    cap, ceiling = 1.50, 10.5
    # Half a bank -> 50% full; pct_of_day reads as 350% at the same balance.
    half = quota.make_status(ceiling / 2, cap, ceiling)
    assert half["pct_full"] == pytest.approx(50.0)
    assert half["pct_of_day"] == pytest.approx(350.0)
    # A full bank caps at 100% even if balance somehow exceeds the ceiling.
    full = quota.make_status(ceiling * 2, cap, ceiling)
    assert full["pct_full"] == 100.0


# --- pricing parity ---------------------------------------------------------

def test_pricing_matches_record_and_usage_extraction():
    rec = {
        "model": "claude-sonnet-4-6-20260101",
        "input_tokens": 1000, "output_tokens": 500,
        "cache_read_tokens": 2000,
        "cache_creation_5m_tokens": 300, "cache_creation_1h_tokens": 100,
        "web_search_requests": 2,
    }
    # Hand-computed at Sonnet base ($3/$15 per M), cache mults, $10/1000 search.
    expected = (
        1000 * 3.0 + 500 * 15.0
        + 2000 * 3.0 * 0.10
        + 300 * 3.0 * 1.25
        + 100 * 3.0 * 2.00
    ) / 1_000_000.0 + 2 * (10.0 / 1000.0)
    assert pricing.api_cost(rec) == pytest.approx(expected)


def test_cost_from_usage_handles_sdk_like_object():
    class _Cache:
        ephemeral_5m_input_tokens = 300
        ephemeral_1h_input_tokens = 100

    class _Server:
        web_search_requests = 1

    class _Usage:
        input_tokens = 1000
        output_tokens = 200
        cache_read_input_tokens = 500
        cache_creation_input_tokens = 400
        cache_creation = _Cache()
        server_tool_use = _Server()

    got = pricing.cost_from_usage("claude-haiku-4-5-20251001", _Usage())
    rec = pricing.record_from_usage("claude-haiku-4-5-20251001", _Usage())
    assert got == pytest.approx(pricing.api_cost(rec))
    assert rec["cache_creation_5m_tokens"] == 300
    assert rec["web_search_requests"] == 1


# --- auth tier migration ----------------------------------------------------

def test_legacy_auth_db_without_tier_defaults_to_light(tmp_path):
    """An auth.sql created before the tier column gets it added with DEFAULT
    'light' on open, so old accounts are grandfathered onto the base tier."""
    from aime import auth

    db = str(tmp_path / "auth.sql")
    # Simulate a pre-tier users table (minimal columns the migration needs).
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE users (id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "username TEXT UNIQUE, password_hash TEXT, salt_dek BLOB, "
        "wrapped_dek_v2 BLOB, enc_version INTEGER)"
    )
    conn.execute(
        "INSERT INTO users (username, password_hash, enc_version) "
        "VALUES ('legacy', 'x', 0)"
    )
    conn.commit()
    conn.close()

    # Opening through the backend runs the additive migrations, including tier.
    backend = auth.LocalAuthBackend(db)
    rec = backend.lookup_by_username("legacy")
    assert rec is not None
    assert rec.tier == "light"


def test_config_tier_daily_cap_falls_back_for_unknown():
    assert config.tier_daily_cap("light") == config.USAGE_TIERS["light"]
    assert config.tier_daily_cap("power") == config.USAGE_TIERS["power"]
    # Unknown / None -> default tier's cap, never zero.
    assert config.tier_daily_cap("platinum") > 0
    assert config.tier_daily_cap(None) > 0
