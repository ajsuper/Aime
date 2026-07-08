"""Tests for Stripe billing (aime.billing + the auth/config/web_app seams).

No network: Stripe is never called. The pure-logic tests exercise the parts that
carry the real correctness risk — the status→api_access / Price→tier reconcile,
user resolution, the dahlia field layout (current_period_end on the item), and
the auth columns/migration. The module-level guards and the webhook route are
exercised in clean subprocesses (so each gets its own AIME_ACCESS_MODE without
the env coupling that an in-process import would carry across the suite).
"""

import os
import sqlite3
import subprocess
import sys
import tempfile
from types import SimpleNamespace

import pytest

from aime import config, billing
from aime.auth import LocalAuthBackend

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Env that makes web_app boot in billing mode with Stripe "configured" (dummy
# values — the actual Stripe SDK is monkeypatched in the snippets that need it).
_BILLING_ENV = {
    "AIME_ACCESS_MODE": "billing",
    "AIME_STRIPE_SECRET_KEY": "sk_test_dummy",
    "AIME_STRIPE_PUBLISHABLE_KEY": "pk_test_dummy",
    "AIME_STRIPE_WEBHOOK_SECRET": "whsec_dummy",
    "AIME_STRIPE_PRICE_LIGHT": "price_light",
    "AIME_STRIPE_PRICE_POWER": "price_power",
    "AIME_PUBLIC_BASE_URL": "http://localhost:5000",
}


# --- fixtures ---------------------------------------------------------------

@pytest.fixture
def backend(tmp_path):
    return LocalAuthBackend(os.path.join(str(tmp_path), "auth", "auth.sql"))


@pytest.fixture
def prices(monkeypatch):
    m = {"light": "price_light", "power": "price_power"}
    monkeypatch.setattr(config, "STRIPE_PRICE_BY_TIER", m)
    return m


def _sub(status="active", price="price_power", customer="cus_1",
         sub_id="sub_1", trial_end=None, metadata=None, period_end=1735689600,
         cancel=False):
    """A minimal Stripe-shaped subscription dict (billing._get reads both dict
    and attribute access, so a plain dict is a faithful stand-in). Note the
    dahlia layout: current_period_end + price live on the *item*."""
    return {
        "id": sub_id, "status": status, "customer": customer,
        "trial_end": trial_end, "cancel_at_period_end": cancel,
        "metadata": metadata or {},
        "items": {"data": [{"current_period_end": period_end,
                            "price": {"id": price}}]},
    }


# --- config price <-> tier map ----------------------------------------------

def test_price_tier_round_trip(prices):
    assert config.stripe_price_for_tier("power") == "price_power"
    assert config.stripe_price_for_tier("light") == "price_light"
    assert config.tier_for_stripe_price("price_power") == "power"
    assert config.tier_for_stripe_price("price_light") == "light"


def test_unknown_price_and_tier_are_none(prices):
    assert config.tier_for_stripe_price("price_nope") is None
    assert config.tier_for_stripe_price(None) is None
    assert config.stripe_price_for_tier("enterprise") is None
    assert config.stripe_price_for_tier(None) is None


def test_stripe_configured(monkeypatch, prices):
    monkeypatch.setattr(config, "STRIPE_SECRET_KEY", "sk")
    monkeypatch.setattr(config, "STRIPE_PUBLISHABLE_KEY", "pk")
    monkeypatch.setattr(config, "STRIPE_WEBHOOK_SECRET", "wh")
    monkeypatch.setattr(config, "PUBLIC_BASE_URL", "https://aime.test")
    assert config.stripe_configured() is True
    monkeypatch.setattr(config, "STRIPE_PUBLISHABLE_KEY", "")
    assert config.stripe_configured() is False  # publishable key required for Stripe.js
    monkeypatch.setattr(config, "STRIPE_PUBLISHABLE_KEY", "pk")
    monkeypatch.setattr(config, "PUBLIC_BASE_URL", "")
    assert config.stripe_configured() is False  # base url required for safe URLs
    monkeypatch.setattr(config, "PUBLIC_BASE_URL", "https://aime.test")
    monkeypatch.setattr(config, "STRIPE_WEBHOOK_SECRET", "")
    assert config.stripe_configured() is False
    monkeypatch.setattr(config, "STRIPE_WEBHOOK_SECRET", "wh")
    monkeypatch.setattr(config, "STRIPE_PRICE_BY_TIER",
                        {"light": "price_light", "power": ""})
    assert config.stripe_configured() is False  # a tier missing its price


# --- auth columns / setters / lookup ----------------------------------------

def test_new_columns_default_null(backend):
    user, _ = backend.create("alice", "Sufficiently-long-pw-1")
    rec = backend.lookup(user.id)
    assert rec.stripe_customer_id is None
    assert rec.stripe_subscription_id is None
    assert rec.subscription_status is None
    assert rec.trial_end is None


def test_set_stripe_customer_and_lookup(backend):
    user, _ = backend.create("alice", "Sufficiently-long-pw-1")
    assert backend.set_stripe_customer(user.id, "cus_abc") is True
    assert backend.lookup(user.id).stripe_customer_id == "cus_abc"
    found = backend.lookup_by_stripe_customer("cus_abc")
    assert found is not None and found.id == user.id
    assert backend.lookup_by_stripe_customer("cus_missing") is None
    assert backend.lookup_by_stripe_customer("") is None


def test_set_subscription_persists(backend):
    user, _ = backend.create("alice", "Sufficiently-long-pw-1")
    backend.set_subscription(user.id, subscription_id="sub_1",
                             status="trialing", trial_end=1735689600)
    rec = backend.lookup(user.id)
    assert rec.stripe_subscription_id == "sub_1"
    assert rec.subscription_status == "trialing"
    assert rec.trial_end == 1735689600  # round-trips as int (INTEGER affinity)


def test_list_users_carries_subscription_fields(backend):
    user, _ = backend.create("alice", "Sufficiently-long-pw-1")
    backend.set_stripe_customer(user.id, "cus_abc")
    backend.set_subscription(user.id, subscription_id="sub_1",
                             status="active", trial_end=None)
    rec = next(u for u in backend.list_users() if u.id == user.id)
    assert rec.subscription_status == "active"
    assert rec.stripe_customer_id == "cus_abc"


def test_trial_used_defaults_false_for_new_account(backend):
    user, _ = backend.create("alice", "Sufficiently-long-pw-1")
    assert backend.lookup(user.id).trial_used is False


def test_set_trial_used_and_by_username(backend):
    user, _ = backend.create("alice", "Sufficiently-long-pw-1")
    assert backend.set_trial_used(user.id, True) is True
    assert backend.lookup(user.id).trial_used is True
    assert backend.set_trial_used_by_username("alice", False) is True
    assert backend.lookup(user.id).trial_used is False
    assert backend.set_trial_used_by_username("ghost", True) is False


def test_mark_all_trial_used_flags_everyone(backend):
    backend.create("alice", "Sufficiently-long-pw-1")
    backend.create("bob", "Sufficiently-long-pw-1")
    assert backend.mark_all_trial_used() == 2
    users = backend.list_users()
    assert users and all(u.trial_used for u in users)  # carried by list_users too


def test_trial_used_migration_backfills_existing_only(backend):
    """The cutover hinge: when trial_used is first added, every PRE-EXISTING row
    is backfilled to 1 (no fresh trial for beta testers) while accounts created
    AFTER the migration default to 0 (still get the trial)."""
    user, _ = backend.create("alice", "Sufficiently-long-pw-1")
    path = backend._db_path
    backend._conn.close()
    conn = sqlite3.connect(path)
    conn.execute("ALTER TABLE users DROP COLUMN trial_used")
    conn.commit()
    conn.close()
    reopened = LocalAuthBackend(path)
    # Pre-existing account: deemed to have used its trial.
    assert reopened.lookup(user.id).trial_used is True
    # A brand-new signup after the migration is still eligible.
    new_user, _ = reopened.create("bob", "Sufficiently-long-pw-1")
    assert reopened.lookup(new_user.id).trial_used is False


def test_migration_adds_columns_on_old_db(backend):
    """Simulate a pre-billing database: drop the new columns + their index, then
    reopen — the migration must re-add them."""
    path = backend._db_path
    backend._conn.close()
    conn = sqlite3.connect(path)
    conn.execute("DROP INDEX IF EXISTS idx_users_stripe_customer")
    new_cols = ("stripe_customer_id", "stripe_subscription_id",
                "subscription_status", "trial_end", "comp_access")
    for col in new_cols:
        conn.execute(f"ALTER TABLE users DROP COLUMN {col}")
    conn.commit()
    cols = {r[1] for r in conn.execute("PRAGMA table_info(users)")}
    conn.close()
    assert "stripe_customer_id" not in cols  # confirm the simulation worked
    assert "comp_access" not in cols

    reopened = LocalAuthBackend(path)
    cols2 = {r[1] for r in reopened._conn.execute("PRAGMA table_info(users)")}
    for col in new_cols:
        assert col in cols2


# --- billing field helpers --------------------------------------------------

def test_price_id_from_item_and_string_price():
    assert billing._price_id(_sub(price="price_x")) == "price_x"
    # price can be a bare id string instead of an object
    bare = _sub()
    bare["items"]["data"][0]["price"] = "price_bare"
    assert billing._price_id(bare) == "price_bare"


def test_current_period_end_prefers_item_then_legacy():
    # dahlia: on the item
    assert billing._current_period_end(_sub(period_end=999)) == 999
    # legacy: top-level fallback when the item lacks it
    legacy = {"current_period_end": 555, "items": {"data": [{"price": {"id": "p"}}]}}
    assert billing._current_period_end(legacy) == 555


# --- reconcile_subscription (the core seam) ---------------------------------

@pytest.fixture
def billing_user(backend):
    """A billing-mode signup: created with no send access, linked to a Stripe
    customer (as ensure_customer would before checkout)."""
    user, _ = backend.create("alice", "Sufficiently-long-pw-1", api_access=False)
    backend.set_stripe_customer(user.id, "cus_1")
    return user


@pytest.mark.parametrize("status", ["trialing", "active"])
def test_granting_status_unlocks_and_sets_tier(backend, prices, billing_user, status):
    ok = billing.reconcile_subscription(
        backend, _sub(status=status, price="price_power", customer="cus_1"))
    assert ok is True
    rec = backend.lookup(billing_user.id)
    assert rec.api_access is True
    assert rec.tier == "power"
    assert rec.subscription_status == status


@pytest.mark.parametrize("status",
                         ["past_due", "canceled", "unpaid", "incomplete_expired", "paused"])
def test_revoking_status_locks(backend, prices, billing_user, status):
    backend.set_api_access(billing_user.id, True)  # was active
    billing.reconcile_subscription(
        backend, _sub(status=status, customer="cus_1"))
    rec = backend.lookup(billing_user.id)
    assert rec.api_access is False
    assert rec.subscription_status == status


def test_incomplete_leaves_access_unchanged(backend, prices, billing_user):
    # Starts locked; 'incomplete' (first payment unresolved) must not grant.
    billing.reconcile_subscription(
        backend, _sub(status="incomplete", customer="cus_1"))
    assert backend.lookup(billing_user.id).api_access is False
    # And must not revoke an already-granted user either.
    backend.set_api_access(billing_user.id, True)
    billing.reconcile_subscription(
        backend, _sub(status="incomplete", customer="cus_1"))
    assert backend.lookup(billing_user.id).api_access is True


def test_unknown_price_leaves_tier(backend, prices, billing_user):
    backend.set_tier(billing_user.id, "light")
    billing.reconcile_subscription(
        backend, _sub(status="active", price="price_unmapped", customer="cus_1"))
    rec = backend.lookup(billing_user.id)
    assert rec.api_access is True          # access still updates
    assert rec.tier == "light"             # tier left as-is


def test_resolve_by_metadata_when_customer_unknown(backend, prices):
    user, _ = backend.create("bob", "Sufficiently-long-pw-1", api_access=False)
    # No stripe_customer_id stored, so customer lookup misses; metadata carries
    # the id as the fallback.
    sub = _sub(status="trialing", customer="cus_unknown",
               metadata={"aime_user_id": str(user.id)})
    assert billing.reconcile_subscription(backend, sub) is True
    assert backend.lookup(user.id).api_access is True


def test_no_user_returns_false(backend, prices):
    sub = _sub(status="active", customer="cus_nobody")
    assert billing.reconcile_subscription(backend, sub) is False


def test_comp_access_sets_both_flags(backend):
    user, _ = backend.create("carol", "Sufficiently-long-pw-1", api_access=False)
    assert backend.set_comp_access(user.id, True) is True
    rec = backend.lookup(user.id)
    assert rec.comp_access is True
    assert rec.api_access is True          # comp opens the send gate
    assert backend.set_comp_access(user.id, False) is True
    rec = backend.lookup(user.id)
    assert rec.comp_access is False
    assert rec.api_access is False         # removing comp closes it


def test_comp_access_by_username(backend):
    user, _ = backend.create("carol", "Sufficiently-long-pw-1", api_access=False)
    assert backend.set_comp_access_by_username("carol", True) is True
    assert backend.lookup(user.id).comp_access is True
    assert backend.set_comp_access_by_username("nobody", True) is False


def test_list_users_carries_comp(backend):
    user, _ = backend.create("carol", "Sufficiently-long-pw-1")
    backend.set_comp_access(user.id, True)
    rec = next(u for u in backend.list_users() if u.id == user.id)
    assert rec.comp_access is True


def test_reconcile_skips_comped_user(backend, prices, billing_user):
    """A comped user must be immune to Stripe: a canceled subscription event
    must NOT revoke their access, and an active one must NOT change their tier."""
    backend.set_tier(billing_user.id, "light")
    backend.set_comp_access(billing_user.id, True)  # admin comp
    # A cancellation that WOULD normally revoke:
    assert billing.reconcile_subscription(
        backend, _sub(status="canceled", customer="cus_1")) is True
    rec = backend.lookup(billing_user.id)
    assert rec.api_access is True   # untouched
    assert rec.comp_access is True
    # An active power subscription must not bump their (admin-chosen) tier:
    billing.reconcile_subscription(
        backend, _sub(status="active", price="price_power", customer="cus_1"))
    assert backend.lookup(billing_user.id).tier == "light"


def test_reconcile_is_idempotent(backend, prices, billing_user):
    sub = _sub(status="active", price="price_power", customer="cus_1")
    billing.reconcile_subscription(backend, sub)
    first = backend.lookup(billing_user.id)
    billing.reconcile_subscription(backend, sub)
    second = backend.lookup(billing_user.id)
    assert (first.api_access, first.tier, first.subscription_status) == \
           (second.api_access, second.tier, second.subscription_status)


# --- live_summary / reconcile_customer (monkeypatched Stripe list) ----------

class _FakeList:
    def __init__(self, data):
        self.data = data

    def __getitem__(self, k):  # billing._get uses dict-style access
        return {"data": self.data}[k]


def test_live_summary_shape(monkeypatch, prices):
    monkeypatch.setattr(billing, "_initialized", True)  # skip init_stripe
    sub = _sub(status="trialing", price="price_light", trial_end=42,
               period_end=99, cancel=True)
    monkeypatch.setattr(billing.stripe.Subscription, "list",
                        lambda **kw: _FakeList([sub]))
    s = billing.live_summary("cus_1")
    assert s == {"has_subscription": True, "status": "trialing",
                 "tier": "light", "trial_end": 42, "current_period_end": 99,
                 "cancel_at_period_end": True,
                 "prices": {}, "default_tier": config.USAGE_DEFAULT_TIER}


def test_live_summary_no_subscription(monkeypatch, prices):
    monkeypatch.setattr(billing, "_initialized", True)
    monkeypatch.setattr(billing.stripe.Subscription, "list",
                        lambda **kw: _FakeList([]))
    s = billing.live_summary("cus_1")
    assert s["has_subscription"] is False


def test_tier_prices_reads_live_and_omits_unreadable(monkeypatch, prices):
    monkeypatch.setattr(config, "STRIPE_SECRET_KEY", "sk_test_x")
    monkeypatch.setattr(billing, "_initialized", True)
    monkeypatch.setattr(billing, "_PRICE_CACHE", {"at": 0.0, "data": None})

    def fake_retrieve(pid):
        if pid == "price_light":
            return {"unit_amount": 800, "currency": "usd",
                    "recurring": {"interval": "month"}}
        raise RuntimeError("no such price")  # power Price unreadable → omitted

    monkeypatch.setattr(billing.stripe.Price, "retrieve", fake_retrieve)
    out = billing.tier_prices(force=True)
    assert out == {"light": {"amount": 800, "currency": "usd",
                             "interval": "month"}}


def test_tier_prices_empty_without_secret(monkeypatch, prices):
    monkeypatch.setattr(config, "STRIPE_SECRET_KEY", "")
    monkeypatch.setattr(billing, "_PRICE_CACHE", {"at": 0.0, "data": None})
    assert billing.tier_prices(force=True) == {}


def test_reconcile_customer_uses_latest(monkeypatch, backend, prices, billing_user):
    monkeypatch.setattr(billing, "_initialized", True)
    sub = _sub(status="active", price="price_power", customer="cus_1")
    monkeypatch.setattr(billing.stripe.Subscription, "list",
                        lambda **kw: _FakeList([sub]))
    assert billing.reconcile_customer(backend, "cus_1") is True
    assert backend.lookup(billing_user.id).api_access is True


# --- self-healing send gate (access_is_stale / refresh_access) --------------

def _u(*, api_access, billing_synced_at, comp_access=False,
       stripe_customer_id="cus_1"):
    """Minimal duck-typed UserRecord for the pure staleness policy."""
    return SimpleNamespace(api_access=api_access, comp_access=comp_access,
                           stripe_customer_id=stripe_customer_id,
                           billing_synced_at=billing_synced_at)


def test_reconcile_stamps_sync_time(backend, prices, billing_user):
    """Every reconcile records billing_synced_at so the gate's staleness check
    can bound how often it re-reads Stripe."""
    assert backend.lookup(billing_user.id).billing_synced_at is None
    billing.reconcile_subscription(backend, _sub(status="active", customer="cus_1"))
    assert backend.lookup(billing_user.id).billing_synced_at is not None


def test_access_is_stale_never_synced():
    # NULL billing_synced_at = maximally stale, so the first gate hit self-heals.
    assert billing.access_is_stale(_u(api_access=False, billing_synced_at=None),
                                   now=10**9) is True
    assert billing.access_is_stale(_u(api_access=True, billing_synced_at=None),
                                   now=10**9) is True


def test_access_is_stale_denied_rechecked_quickly():
    last = 1000
    short = billing.ACCESS_RECHECK_DENIED_SECONDS
    assert billing.access_is_stale(_u(api_access=False, billing_synced_at=last),
                                   now=last + short - 1) is False
    assert billing.access_is_stale(_u(api_access=False, billing_synced_at=last),
                                   now=last + short) is True


def test_access_is_stale_granted_rechecked_lazily():
    last = 1000
    short = billing.ACCESS_RECHECK_DENIED_SECONDS
    long = billing.ACCESS_RECHECK_GRANTED_SECONDS
    # A granted user is NOT re-checked on the short (denied) cadence...
    assert billing.access_is_stale(_u(api_access=True, billing_synced_at=last),
                                   now=last + short + 5) is False
    # ...only after the long cadence.
    assert billing.access_is_stale(_u(api_access=True, billing_synced_at=last),
                                   now=last + long) is True


def test_access_is_stale_skips_comp_and_no_customer():
    # Comped: admin owns access, nothing to learn from Stripe.
    assert billing.access_is_stale(
        _u(api_access=False, billing_synced_at=None, comp_access=True),
        now=10**9) is False
    # Never started checkout: no customer to reconcile against.
    assert billing.access_is_stale(
        _u(api_access=False, billing_synced_at=None, stripe_customer_id=None),
        now=10**9) is False


def test_refresh_access_grants_from_live_stripe(monkeypatch, backend, prices, billing_user):
    """A user locked out by a missed webhook is unlocked by a live read, and the
    sync time is stamped so the gate won't re-poll Stripe immediately."""
    monkeypatch.setattr(billing, "_initialized", True)
    sub = _sub(status="trialing", price="price_power", customer="cus_1")
    monkeypatch.setattr(billing.stripe.Subscription, "list",
                        lambda **kw: _FakeList([sub]))
    user = backend.lookup(billing_user.id)
    assert user.api_access is False
    assert billing.refresh_access(backend, user) is True
    fresh = backend.lookup(billing_user.id)
    assert fresh.api_access is True
    assert fresh.billing_synced_at is not None


def test_refresh_access_keeps_cache_on_stripe_error(monkeypatch, backend, prices, billing_user):
    """A Stripe outage must not lock out a currently-granted user nor wipe the
    cache; refresh returns cached access and leaves synced_at unstamped (retry)."""
    backend.set_api_access(billing_user.id, True)
    monkeypatch.setattr(billing, "_initialized", True)

    def boom(**kw):
        raise RuntimeError("stripe down")
    monkeypatch.setattr(billing.stripe.Subscription, "list", boom)
    user = backend.lookup(billing_user.id)
    assert billing.refresh_access(backend, user) is True   # cached grant kept
    fresh = backend.lookup(billing_user.id)
    assert fresh.api_access is True
    assert fresh.billing_synced_at is None                 # unstamped → retries


def test_refresh_access_stamps_when_no_subscription(monkeypatch, backend, prices, billing_user):
    """A customer with no subscription is a definitive 'no access' — stamp it so
    a non-paying user isn't re-polled against Stripe on every request."""
    monkeypatch.setattr(billing, "_initialized", True)
    monkeypatch.setattr(billing.stripe.Subscription, "list",
                        lambda **kw: _FakeList([]))
    user = backend.lookup(billing_user.id)
    assert billing.refresh_access(backend, user) is False
    assert backend.lookup(billing_user.id).billing_synced_at is not None


# --- anti-abuse: subscription_state + cancel/resume --------------------------

def test_subscription_state(monkeypatch, prices):
    monkeypatch.setattr(billing, "_initialized", True)
    def with_subs(subs):
        monkeypatch.setattr(billing.stripe.Subscription, "list",
                            lambda **kw: _FakeList(subs))
    with_subs([])
    assert billing.subscription_state("c") == {"has_active": False, "used_trial": False}
    with_subs([_sub(status="active", trial_end=123)])
    assert billing.subscription_state("c") == {"has_active": True, "used_trial": True}
    # A canceled subscription that once had a trial: not active, but the trial
    # is spent — so a re-subscribe must NOT get another trial.
    with_subs([_sub(status="canceled", trial_end=99)])
    assert billing.subscription_state("c") == {"has_active": False, "used_trial": True}
    # Active, never trialed (e.g. paid immediately): active, no trial used.
    with_subs([_sub(status="active", trial_end=None)])
    assert billing.subscription_state("c") == {"has_active": True, "used_trial": False}


def test_cancel_subscriptions_schedules_period_end(monkeypatch, prices):
    monkeypatch.setattr(billing, "_initialized", True)
    subs = [_sub(sub_id="sub_a", status="active", cancel=False),
            _sub(sub_id="sub_b", status="canceled", cancel=False),   # terminal: skip
            _sub(sub_id="sub_c", status="active", cancel=True)]       # already set: skip
    monkeypatch.setattr(billing.stripe.Subscription, "list",
                        lambda **kw: _FakeList(subs))
    calls = []
    monkeypatch.setattr(billing.stripe.Subscription, "modify",
                        lambda sid, **kw: calls.append((sid, kw)))
    assert billing.cancel_subscriptions("c") == 1
    assert calls == [("sub_a", {"cancel_at_period_end": True})]


def test_resume_subscriptions_clears_cancel(monkeypatch, prices):
    monkeypatch.setattr(billing, "_initialized", True)
    subs = [_sub(sub_id="sub_a", status="active", cancel=True),
            _sub(sub_id="sub_b", status="active", cancel=False)]      # nothing to undo
    monkeypatch.setattr(billing.stripe.Subscription, "list",
                        lambda **kw: _FakeList(subs))
    calls = []
    monkeypatch.setattr(billing.stripe.Subscription, "modify",
                        lambda sid, **kw: calls.append((sid, kw)))
    assert billing.resume_subscriptions("c") == 1
    assert calls == [("sub_a", {"cancel_at_period_end": False})]


# --- inline management: change plan / update card ----------------------------

def _sub_with_item(item_id="si_1", **kw):
    """_sub plus an id on the subscription item (Stripe items carry an si_… id;
    change_plan needs it to target the right item when swapping the Price)."""
    sub = _sub(**kw)
    sub["items"]["data"][0]["id"] = item_id
    return sub


def test_change_plan_swaps_price_with_proration(monkeypatch, prices):
    monkeypatch.setattr(billing, "_initialized", True)
    sub = _sub_with_item(sub_id="sub_a", status="active", price="price_light")
    monkeypatch.setattr(billing.stripe.Subscription, "list",
                        lambda **kw: _FakeList([sub]))
    calls = []
    monkeypatch.setattr(billing.stripe.Subscription, "modify",
                        lambda sid, **kw: calls.append((sid, kw)))
    billing.change_plan("c", "power")
    assert calls == [("sub_a", {
        "items": [{"id": "si_1", "price": "price_power"}],
        "proration_behavior": "create_prorations",
    })]


def test_change_plan_noop_when_already_on_tier(monkeypatch, prices):
    monkeypatch.setattr(billing, "_initialized", True)
    sub = _sub_with_item(status="active", price="price_power")
    monkeypatch.setattr(billing.stripe.Subscription, "list",
                        lambda **kw: _FakeList([sub]))
    def boom(*a, **k): raise AssertionError("must not modify when unchanged")
    monkeypatch.setattr(billing.stripe.Subscription, "modify", boom)
    billing.change_plan("c", "power")  # price_power → power: no-op, no modify


def test_change_plan_no_live_subscription_raises(monkeypatch, prices):
    monkeypatch.setattr(billing, "_initialized", True)
    monkeypatch.setattr(billing.stripe.Subscription, "list",
                        lambda **kw: _FakeList([_sub(status="canceled")]))
    with pytest.raises(ValueError):
        billing.change_plan("c", "power")


def test_change_plan_unknown_tier_raises(monkeypatch, prices):
    monkeypatch.setattr(billing, "_initialized", True)
    with pytest.raises(ValueError):
        billing.change_plan("c", "nope")


def test_update_payment_method_sets_defaults(monkeypatch, prices):
    monkeypatch.setattr(billing, "_initialized", True)
    # saved_payment_method resolves the SetupIntent → (pm, tier); stub it.
    monkeypatch.setattr(billing, "saved_payment_method",
                        lambda si, cid: ("pm_new", ""))
    sub = _sub(sub_id="sub_a", status="active")
    monkeypatch.setattr(billing.stripe.Subscription, "list",
                        lambda **kw: _FakeList([sub]))
    cust_calls, sub_calls = [], []
    monkeypatch.setattr(billing.stripe.Customer, "modify",
                        lambda cid, **kw: cust_calls.append((cid, kw)))
    monkeypatch.setattr(billing.stripe.Subscription, "modify",
                        lambda sid, **kw: sub_calls.append((sid, kw)))
    billing.update_payment_method("cus_1", "seti_1")
    assert cust_calls == [("cus_1",
        {"invoice_settings": {"default_payment_method": "pm_new"}})]
    assert sub_calls == [("sub_a", {"default_payment_method": "pm_new"})]


def test_update_payment_method_rejects_unconfirmed(monkeypatch, prices):
    monkeypatch.setattr(billing, "_initialized", True)
    def reject(si, cid): raise ValueError("card not confirmed")
    monkeypatch.setattr(billing, "saved_payment_method", reject)
    with pytest.raises(ValueError):
        billing.update_payment_method("cus_1", "seti_bad")


# --- inline subscribe: the two-step Payment Element helpers ------------------
# create_setup_intent (collect the card) → saved_payment_method (verify the
# confirmed card is ours) → create_subscription (start it on that card). These
# carry the real correctness risk in the inline flow, so they're stubbed at the
# Stripe SDK boundary (same pattern as the subscription_state tests above).

def test_create_setup_intent(monkeypatch, prices):
    monkeypatch.setattr(billing, "_initialized", True)  # skip init_stripe
    seen = {}
    monkeypatch.setattr(
        billing.stripe.SetupIntent, "create",
        lambda **kw: seen.update(kw) or {"id": "seti_1",
                                         "client_secret": "seti_secret"})
    out = billing.create_setup_intent(customer_id="cus_1", tier="power")
    assert out == {"client_secret": "seti_secret", "setup_intent_id": "seti_1"}
    # Bound to the customer (so the saved card lands on the right account),
    # collected for later off-session charges, and the chosen tier rides on the
    # metadata to be read back server-side at confirm (never from the client).
    assert seen["customer"] == "cus_1"
    assert seen["usage"] == "off_session"
    assert seen["metadata"] == {"aime_tier": "power"}
    assert seen["automatic_payment_methods"] == {"enabled": True}


def test_saved_payment_method_success(monkeypatch):
    monkeypatch.setattr(billing, "_initialized", True)
    monkeypatch.setattr(
        billing.stripe.SetupIntent, "retrieve",
        lambda sid: {"customer": "cus_1", "status": "succeeded",
                     "payment_method": "pm_1", "metadata": {"aime_tier": "power"}})
    assert billing.saved_payment_method("seti_1", "cus_1") == ("pm_1", "power")


def test_saved_payment_method_resolves_expanded_objects(monkeypatch):
    """customer / payment_method arrive as nested objects when expanded — they
    must be resolved down to their ids."""
    monkeypatch.setattr(billing, "_initialized", True)
    monkeypatch.setattr(
        billing.stripe.SetupIntent, "retrieve",
        lambda sid: {"customer": {"id": "cus_1"}, "status": "succeeded",
                     "payment_method": {"id": "pm_1"},
                     "metadata": {"aime_tier": "light"}})
    assert billing.saved_payment_method("seti_1", "cus_1") == ("pm_1", "light")


@pytest.mark.parametrize("intent, why", [
    ({"customer": "cus_other", "status": "succeeded", "payment_method": "pm_1"},
     "a SetupIntent for a different customer"),
    ({"customer": "cus_1", "status": "requires_payment_method",
      "payment_method": "pm_1"}, "a card that was never confirmed"),
    ({"customer": "cus_1", "status": "succeeded", "payment_method": None},
     "a succeeded intent with no payment method"),
])
def test_saved_payment_method_rejects(monkeypatch, intent, why):
    """Anything that isn't a *succeeded* SetupIntent for THIS customer carrying a
    payment method must raise — the confirm route turns that into a 400 and never
    creates a subscription on an unconfirmed or foreign card."""
    monkeypatch.setattr(billing, "_initialized", True)
    monkeypatch.setattr(billing.stripe.SetupIntent, "retrieve", lambda sid: intent)
    with pytest.raises(ValueError):
        billing.saved_payment_method("seti_1", "cus_1")


def test_create_subscription_trial(monkeypatch, prices):
    """A first-time subscriber: the trial is set, the confirmed card is the
    subscription default, and missing_payment_method=cancel guards a card-less
    conversion. Nothing is charged now, so no payment_behavior override."""
    monkeypatch.setattr(billing, "_initialized", True)
    seen = {}
    monkeypatch.setattr(billing.stripe.Subscription, "create",
                        lambda **kw: seen.update(kw) or {"id": "sub_new"})
    sid = billing.create_subscription(
        user_id=7, customer_id="cus_1", tier="power",
        payment_method_id="pm_1", trial_days=30)
    assert sid == "sub_new"
    assert seen["customer"] == "cus_1"
    assert seen["items"] == [{"price": "price_power"}]
    assert seen["default_payment_method"] == "pm_1"
    assert seen["metadata"] == {"aime_user_id": "7"}
    assert seen["off_session"] is True
    assert seen["trial_period_days"] == 30
    assert seen["trial_settings"] == {
        "end_behavior": {"missing_payment_method": "cancel"}}
    assert "payment_behavior" not in seen


def test_create_subscription_no_trial_charges_now(monkeypatch, prices):
    """A returning customer who already used their trial: no trial, and the first
    invoice is charged immediately with error_if_incomplete so a decline raises
    here instead of leaving a stuck incomplete subscription."""
    monkeypatch.setattr(billing, "_initialized", True)
    seen = {}
    monkeypatch.setattr(billing.stripe.Subscription, "create",
                        lambda **kw: seen.update(kw) or {"id": "sub_new"})
    billing.create_subscription(
        user_id=7, customer_id="cus_1", tier="light",
        payment_method_id="pm_1", trial_days=0)
    assert seen["payment_behavior"] == "error_if_incomplete"
    assert "trial_period_days" not in seen
    assert "trial_settings" not in seen


def test_create_subscription_unknown_tier_raises(monkeypatch, prices):
    monkeypatch.setattr(billing, "_initialized", True)
    monkeypatch.setattr(billing.stripe.Subscription, "create",
                        lambda **kw: {"id": "sub_new"})
    with pytest.raises(ValueError):
        billing.create_subscription(
            user_id=7, customer_id="cus_1", tier="enterprise",
            payment_method_id="pm_1", trial_days=30)


# --- ensure_customer / create_portal_session --------------------------------

def test_ensure_customer_creates_and_persists(monkeypatch, backend):
    monkeypatch.setattr(billing, "_initialized", True)
    user, _ = backend.create("bob", "Sufficiently-long-pw-1")
    seen = {}
    monkeypatch.setattr(
        billing.stripe.Customer, "create",
        lambda **kw: seen.update(kw) or SimpleNamespace(id="cus_new"))
    cid = billing.ensure_customer(backend, backend.lookup(user.id))
    assert cid == "cus_new"
    # Persisted *before* the card flow so a later webhook can resolve the
    # subscription back to this user.
    assert backend.lookup_by_stripe_customer("cus_new").id == user.id
    assert seen["metadata"]["aime_user_id"] == str(user.id)


def test_ensure_customer_reuses_existing(monkeypatch, backend, billing_user):
    """A user already linked to a Stripe customer never creates a second one."""
    monkeypatch.setattr(billing, "_initialized", True)

    def boom(**kw):
        raise AssertionError("must not create a second customer")

    monkeypatch.setattr(billing.stripe.Customer, "create", boom)
    assert billing.ensure_customer(
        backend, backend.lookup(billing_user.id)) == "cus_1"


def test_create_portal_session(monkeypatch):
    monkeypatch.setattr(billing, "_initialized", True)
    seen = {}
    monkeypatch.setattr(
        billing.stripe.billing_portal.Session, "create",
        lambda **kw: seen.update(kw) or SimpleNamespace(url="https://portal"))
    url = billing.create_portal_session(
        customer_id="cus_1", return_url="https://app/")
    assert url == "https://portal"
    assert seen == {"customer": "cus_1", "return_url": "https://app/"}


# --- web_app guards + webhook route (subprocess, clean env per scenario) -----

def _run_snippet(env_extra, snippet):
    env = dict(os.environ)
    env.update(env_extra)
    env["AIME_DATABASE_DIR"] = tempfile.mkdtemp()
    env.setdefault("AIME_ALLOW_SIGNUP", "1")
    full = "import sys; sys.path.insert(0, 'src')\n" + snippet
    return subprocess.run([sys.executable, "-c", full], cwd=_REPO,
                          capture_output=True, text=True, env=env)


def test_fail_closed_startup_without_stripe():
    """billing mode + no Stripe config must refuse to start."""
    proc = _run_snippet(
        {"AIME_ACCESS_MODE": "billing"},
        "import frontends.web_app\n",
    )
    assert proc.returncode != 0
    assert "Refusing to start" in proc.stderr


def test_starts_when_billing_configured():
    proc = _run_snippet(
        _BILLING_ENV,
        "import frontends.web_app as w; assert w._billing_armed(); print('OK')",
    )
    assert proc.returncode == 0, proc.stderr
    assert "OK" in proc.stdout


def test_webhook_404_when_not_billing_mode():
    """The webhook has no login_required, so in keys mode it 404s outright —
    a clean check that /billing/* is gated off outside billing mode."""
    proc = _run_snippet(
        {"AIME_ACCESS_MODE": "keys"},
        "import frontends.web_app as w\n"
        "c = w.app.test_client()\n"
        "r = c.post('/billing/webhook', data=b'{}')\n"
        "assert r.status_code == 404, r.status_code\n"
        "print('OK')\n",
    )
    assert proc.returncode == 0, proc.stderr + proc.stdout
    assert "OK" in proc.stdout


def test_webhook_bad_signature_400():
    proc = _run_snippet(
        _BILLING_ENV,
        "import frontends.web_app as w\n"
        "def boom(payload, sig):\n"
        "    raise ValueError('bad sig')\n"
        "w._billing.construct_event = boom\n"
        "c = w.app.test_client()\n"
        "r = c.post('/billing/webhook', data=b'{}', headers={'Stripe-Signature':'x'})\n"
        "assert r.status_code == 400, r.status_code\n"
        "print('OK')\n",
    )
    assert proc.returncode == 0, proc.stderr + proc.stdout
    assert "OK" in proc.stdout


def test_webhook_refetches_and_reconciles():
    """A good subscription.updated event must re-fetch the subscription (not
    trust the event body) and reconcile it."""
    proc = _run_snippet(
        _BILLING_ENV,
        "import frontends.web_app as w\n"
        "seen = {}\n"
        "w._billing.construct_event = lambda p, s: "
        "{'type':'customer.subscription.updated','data':{'object':{'id':'sub_1'}}}\n"
        "def fake_retrieve(i):\n"
        "    seen['retrieved'] = i\n"
        "    return {'id': i, 'status': 'active', 'customer': 'cus_x'}\n"
        "w._billing.retrieve_subscription = fake_retrieve\n"
        "def fake_reconcile(ab, sub):\n"
        "    seen['reconciled'] = sub['id']\n"
        "    return True\n"
        "w._billing.reconcile_subscription = fake_reconcile\n"
        "c = w.app.test_client()\n"
        "r = c.post('/billing/webhook', data=b'{}', headers={'Stripe-Signature':'x'})\n"
        "assert r.status_code == 200, r.status_code\n"
        "assert seen.get('retrieved') == 'sub_1', seen\n"
        "assert seen.get('reconciled') == 'sub_1', seen\n"
        "print('OK')\n",
    )
    assert proc.returncode == 0, proc.stderr + proc.stdout
    assert "OK" in proc.stdout


def test_fail_closed_without_public_base_url():
    """billing mode with Stripe keys but no AIME_PUBLIC_BASE_URL must refuse to
    start (return URLs would otherwise be built from the Host header)."""
    env = dict(_BILLING_ENV)
    env.pop("AIME_PUBLIC_BASE_URL")
    proc = _run_snippet(env, "import frontends.web_app\n")
    assert proc.returncode != 0
    assert "Refusing to start" in proc.stderr


def test_webhook_500_on_handler_error():
    """An unexpected handler failure must return 500 so Stripe retries (the only
    safety net against a lost grant/revoke)."""
    proc = _run_snippet(
        _BILLING_ENV,
        "import frontends.web_app as w\n"
        "w._billing.construct_event = lambda p, s: "
        "{'type':'customer.subscription.deleted','data':{'object':{'id':'sub_1'}}}\n"
        "def boom(ab, sub):\n"
        "    raise RuntimeError('db locked')\n"
        "w._billing.reconcile_subscription = boom\n"
        "c = w.app.test_client()\n"
        "r = c.post('/billing/webhook', data=b'{}', headers={'Stripe-Signature':'x'})\n"
        "assert r.status_code == 500, r.status_code\n"
        "print('OK')\n",
    )
    assert proc.returncode == 0, proc.stderr + proc.stdout
    assert "OK" in proc.stdout


def test_subscribe_blocks_second_subscription():
    """Step 1 (/billing/subscribe) refuses to start a card flow when the
    customer already has a live subscription."""
    proc = _run_snippet(
        _BILLING_ENV,
        "import frontends.web_app as w\n"
        "c = w.app.test_client()\n"
        "c.post('/signup', data={'username':'owner','password':'Sufficiently-long-pw-1','password2':'Sufficiently-long-pw-1'})\n"
        "w._billing.ensure_customer = lambda ab, u: 'cus_1'\n"
        "w._billing.subscription_state = lambda cid: {'has_active': True, 'used_trial': True}\n"
        "r = c.post('/billing/subscribe', json={'tier':'power'})\n"
        "assert r.status_code == 409, r.status_code\n"
        "assert r.get_json()['code'] == 'already_subscribed'\n"
        "print('OK')\n",
    )
    assert proc.returncode == 0, proc.stderr + proc.stdout
    assert "OK" in proc.stdout


def test_subscribe_returns_client_secret():
    """Step 1 hands back the SetupIntent client secret + publishable key for the
    inline Payment Element."""
    proc = _run_snippet(
        _BILLING_ENV,
        "import frontends.web_app as w\n"
        "c = w.app.test_client()\n"
        "c.post('/signup', data={'username':'owner','password':'Sufficiently-long-pw-1','password2':'Sufficiently-long-pw-1'})\n"
        "w._billing.ensure_customer = lambda ab, u: 'cus_1'\n"
        "w._billing.subscription_state = lambda cid: {'has_active': False, 'used_trial': False}\n"
        "w._billing.create_setup_intent = lambda **k: {'client_secret': 'seti_secret', 'setup_intent_id': 'seti_1'}\n"
        "r = c.post('/billing/subscribe', json={'tier':'power'})\n"
        "assert r.status_code == 200, r.status_code\n"
        "j = r.get_json()\n"
        "assert j['client_secret'] == 'seti_secret', j\n"
        "assert j['publishable_key'] == 'pk_test_dummy', j\n"
        "print('OK')\n",
    )
    assert proc.returncode == 0, proc.stderr + proc.stdout
    assert "OK" in proc.stdout


def test_subscribe_forces_default_tier():
    """No plan picker at signup: /billing/subscribe always opens the trial on the
    default tier, ignoring any tier the client tries to send (so a trial can't be
    started on the pricier plan)."""
    proc = _run_snippet(
        _BILLING_ENV,
        "import frontends.web_app as w\n"
        "c = w.app.test_client()\n"
        "c.post('/signup', data={'username':'owner','password':'Sufficiently-long-pw-1','password2':'Sufficiently-long-pw-1'})\n"
        "w._billing.ensure_customer = lambda ab, u: 'cus_1'\n"
        "w._billing.subscription_state = lambda cid: {'has_active': False, 'used_trial': False}\n"
        "seen = {}\n"
        "w._billing.create_setup_intent = lambda **k: (seen.update(k) or {'client_secret':'s','setup_intent_id':'seti_1'})\n"
        "r = c.post('/billing/subscribe', json={'tier':'power'})\n"
        "assert r.status_code == 200, r.status_code\n"
        "assert seen['tier'] == w.aime_config.USAGE_DEFAULT_TIER, seen\n"
        "assert seen['tier'] != 'power', seen\n"
        "print('OK')\n",
    )
    assert proc.returncode == 0, proc.stderr + proc.stdout
    assert "OK" in proc.stdout


def test_subscribe_confirm_denies_trial_when_flagged():
    """A trial-ineligible account (e.g. a beta tester flagged at cutover) is
    charged immediately even though Stripe sees no prior trial: trial_used ORs
    into the eligibility decision, so trial_days is 0."""
    proc = _run_snippet(
        _BILLING_ENV,
        "import frontends.web_app as w\n"
        "c = w.app.test_client()\n"
        "c.post('/signup', data={'username':'owner','password':'Sufficiently-long-pw-1','password2':'Sufficiently-long-pw-1'})\n"
        "w._auth_backend.set_stripe_customer(1, 'cus_1')\n"
        "w._auth_backend.set_trial_used_by_username('owner', True)\n"
        "w._billing.saved_payment_method = lambda sid, cid: ('pm_1', 'power')\n"
        "seen = {}\n"
        "w._billing.create_subscription = lambda **k: (seen.update(k) or 'sub_1')\n"
        "w._billing.subscription_state = lambda cid: {'has_active': False, 'used_trial': False}\n"
        "w._billing.reconcile_customer = lambda ab, cid: None\n"
        "r = c.post('/billing/subscribe/confirm', json={'setup_intent_id':'seti_1'})\n"
        "assert r.status_code == 200, (r.status_code, r.get_json())\n"
        "assert seen['trial_days'] == 0, seen\n"
        "print('OK')\n",
    )
    assert proc.returncode == 0, proc.stderr + proc.stdout
    assert "OK" in proc.stdout


def test_me_reports_trial_eligibility():
    """/me's billing block tells the frontend whether to say 'Start your free
    trial' (eligible) or 'Subscribe' (flagged ineligible)."""
    proc = _run_snippet(
        _BILLING_ENV,
        "import frontends.web_app as w\n"
        "c = w.app.test_client()\n"
        "c.post('/signup', data={'username':'owner','password':'Sufficiently-long-pw-1','password2':'Sufficiently-long-pw-1'})\n"
        "assert c.get('/me').get_json()['billing']['trial_eligible'] is True\n"
        "w._auth_backend.set_trial_used_by_username('owner', True)\n"
        "assert c.get('/me').get_json()['billing']['trial_eligible'] is False\n"
        "print('OK')\n",
    )
    assert proc.returncode == 0, proc.stderr + proc.stdout
    assert "OK" in proc.stdout


def test_subscribe_confirm_trial_gate():
    """Step 2 (/billing/subscribe/confirm) reads the tier off the SetupIntent,
    grants the trial to a first-time customer and withholds it from one who
    already used it. The card-confirmation guard lives in saved_payment_method."""
    proc = _run_snippet(
        _BILLING_ENV,
        "import frontends.web_app as w\n"
        "c = w.app.test_client()\n"
        "c.post('/signup', data={'username':'owner','password':'Sufficiently-long-pw-1','password2':'Sufficiently-long-pw-1'})\n"
        "w._auth_backend.set_stripe_customer(1, 'cus_1')\n"
        "w._billing.saved_payment_method = lambda sid, cid: ('pm_1', 'power')\n"
        "seen = {}\n"
        "w._billing.create_subscription = lambda **k: (seen.update(k) or 'sub_1')\n"
        "w._billing.subscription_state = lambda cid: {'has_active': False, 'used_trial': False}\n"
        "r = c.post('/billing/subscribe/confirm', json={'setup_intent_id':'seti_1'})\n"
        "assert r.status_code == 200, (r.status_code, r.get_json())\n"
        "assert seen['trial_days'] == w.aime_config.STRIPE_TRIAL_DAYS, seen\n"
        "assert seen['payment_method_id'] == 'pm_1', seen\n"
        "assert seen['tier'] == 'power', seen\n"
        "w._billing.subscription_state = lambda cid: {'has_active': False, 'used_trial': True}\n"
        "assert c.post('/billing/subscribe/confirm', json={'setup_intent_id':'seti_1'}).status_code == 200\n"
        "assert seen['trial_days'] == 0, seen\n"
        "print('OK')\n",
    )
    assert proc.returncode == 0, proc.stderr + proc.stdout
    assert "OK" in proc.stdout


def test_subscribe_confirm_grants_access_immediately():
    """Step 2 reconciles off a live Stripe read right after creating the
    subscription, so send access (api_access) opens at once instead of hanging on
    the webhook round-trip — the fix for "trial started but chat still locked"
    when the webhook is misconfigured (the classic sandbox case)."""
    proc = _run_snippet(
        _BILLING_ENV,
        "import frontends.web_app as w\n"
        "c = w.app.test_client()\n"
        "c.post('/signup', data={'username':'owner','password':'Sufficiently-long-pw-1','password2':'Sufficiently-long-pw-1'})\n"
        "w._auth_backend.set_stripe_customer(1, 'cus_1')\n"
        "assert w._auth_backend.lookup(1).api_access is False\n"
        "w._billing.saved_payment_method = lambda sid, cid: ('pm_1', 'power')\n"
        "w._billing.subscription_state = lambda cid: {'has_active': False, 'used_trial': False}\n"
        "w._billing.create_subscription = lambda **k: 'sub_1'\n"
        "seen = {}\n"
        "def fake_reconcile(ab, cid):\n"
        "    seen['cid'] = cid\n"
        "    ab.set_api_access(1, True)\n"
        "    return True\n"
        "w._billing.reconcile_customer = fake_reconcile\n"
        "r = c.post('/billing/subscribe/confirm', json={'setup_intent_id':'seti_1'})\n"
        "assert r.status_code == 200, (r.status_code, r.get_json())\n"
        "assert seen.get('cid') == 'cus_1', seen\n"
        "assert w._auth_backend.lookup(1).api_access is True\n"
        "print('OK')\n",
    )
    assert proc.returncode == 0, proc.stderr + proc.stdout
    assert "OK" in proc.stdout


def test_subscribe_confirm_succeeds_when_reconcile_fails():
    """The fast-path reconcile is best-effort: if the live read blips, the
    subscription still exists (and the webhook will grant access), so confirm
    must not fail the request."""
    proc = _run_snippet(
        _BILLING_ENV,
        "import frontends.web_app as w\n"
        "c = w.app.test_client()\n"
        "c.post('/signup', data={'username':'owner','password':'Sufficiently-long-pw-1','password2':'Sufficiently-long-pw-1'})\n"
        "w._auth_backend.set_stripe_customer(1, 'cus_1')\n"
        "w._billing.saved_payment_method = lambda sid, cid: ('pm_1', 'power')\n"
        "w._billing.subscription_state = lambda cid: {'has_active': False, 'used_trial': False}\n"
        "w._billing.create_subscription = lambda **k: 'sub_1'\n"
        "def boom(ab, cid):\n"
        "    raise RuntimeError('stripe blip')\n"
        "w._billing.reconcile_customer = boom\n"
        "r = c.post('/billing/subscribe/confirm', json={'setup_intent_id':'seti_1'})\n"
        "assert r.status_code == 200, (r.status_code, r.get_json())\n"
        "print('OK')\n",
    )
    assert proc.returncode == 0, proc.stderr + proc.stdout
    assert "OK" in proc.stdout


def test_send_gate_self_heals_paying_user():
    """The reliability guarantee end-to-end: a billing user whose grant webhook
    was lost (api_access still 0) but who IS trialing in Stripe is let through
    the send gate, which re-derives access from a live read before refusing."""
    proc = _run_snippet(
        _BILLING_ENV,
        "import frontends.web_app as w\n"
        "c = w.app.test_client()\n"
        "c.post('/signup', data={'username':'owner','password':'Sufficiently-long-pw-1','password2':'Sufficiently-long-pw-1'})\n"
        "w._auth_backend.set_stripe_customer(1, 'cus_1')\n"
        "assert w._auth_backend.lookup(1).api_access is False\n"
        "calls = {'n': 0}\n"
        "def live_says_trialing(ab, cid):\n"
        "    calls['n'] += 1\n"
        "    ab.set_api_access(1, True)\n"   # what reconcile would do off a trialing sub
        "    return True\n"
        "w._billing.reconcile_customer = live_says_trialing\n"
        # Empty /send passes the gate (self-heal grants) and only then fails body
        # validation with 'empty' — proving the gate no longer blocked it.
        "r = c.post('/send', json={})\n"
        "assert r.status_code == 400 and r.get_json().get('error') == 'empty', (r.status_code, r.get_json())\n"
        "assert w._auth_backend.lookup(1).api_access is True\n"
        "assert calls['n'] == 1, calls\n"
        # Second send within the TTL must NOT hit Stripe again (now granted+fresh).
        "c.post('/send', json={})\n"
        "assert calls['n'] == 1, calls\n"
        "print('OK')\n",
    )
    assert proc.returncode == 0, proc.stderr + proc.stdout
    assert "OK" in proc.stdout


def test_send_gate_denies_non_paying_user_with_billing_message():
    """A billing user who really has no subscription stays blocked, with copy
    that points at the trial (not the keys-mode invite-key prompt)."""
    proc = _run_snippet(
        _BILLING_ENV,
        "import frontends.web_app as w\n"
        "c = w.app.test_client()\n"
        "c.post('/signup', data={'username':'owner','password':'Sufficiently-long-pw-1','password2':'Sufficiently-long-pw-1'})\n"
        "w._auth_backend.set_stripe_customer(1, 'cus_1')\n"
        "w._billing.reconcile_customer = lambda ab, cid: False\n"   # no subscription
        "r = c.post('/send', json={})\n"
        "assert r.status_code == 403, r.status_code\n"
        "j = r.get_json()\n"
        "assert j['error'] == 'no_access', j\n"
        "assert 'trial' in j['message'].lower(), j\n"
        "print('OK')\n",
    )
    assert proc.returncode == 0, proc.stderr + proc.stdout
    assert "OK" in proc.stdout


def test_me_self_heals_paying_user():
    """refreshMe polls /me, so /me also self-heals: a paying-but-locked user's
    composer unlocks (api_access flips true) without a send attempt."""
    proc = _run_snippet(
        _BILLING_ENV,
        "import frontends.web_app as w\n"
        "c = w.app.test_client()\n"
        "c.post('/signup', data={'username':'owner','password':'Sufficiently-long-pw-1','password2':'Sufficiently-long-pw-1'})\n"
        "w._auth_backend.set_stripe_customer(1, 'cus_1')\n"
        "w._billing.reconcile_customer = lambda ab, cid: (ab.set_api_access(1, True) or True)\n"
        "r = c.get('/me')\n"
        "assert r.get_json()['api_access'] is True, r.get_json()\n"
        "print('OK')\n",
    )
    assert proc.returncode == 0, proc.stderr + proc.stdout
    assert "OK" in proc.stdout


def test_subscribe_confirm_rejects_unconfirmed_card():
    """If the SetupIntent isn't confirmed/ours, no subscription is created."""
    proc = _run_snippet(
        _BILLING_ENV,
        "import frontends.web_app as w\n"
        "c = w.app.test_client()\n"
        "c.post('/signup', data={'username':'owner','password':'Sufficiently-long-pw-1','password2':'Sufficiently-long-pw-1'})\n"
        "w._auth_backend.set_stripe_customer(1, 'cus_1')\n"
        "def boom(sid, cid):\n"
        "    raise ValueError('card has not been confirmed yet')\n"
        "w._billing.saved_payment_method = boom\n"
        "made = []\n"
        "w._billing.create_subscription = lambda **k: made.append(k)\n"
        "r = c.post('/billing/subscribe/confirm', json={'setup_intent_id':'seti_1'})\n"
        "assert r.status_code == 400, r.status_code\n"
        "assert made == [], made\n"
        "print('OK')\n",
    )
    assert proc.returncode == 0, proc.stderr + proc.stdout
    assert "OK" in proc.stdout


def test_account_delete_cancels_subscription():
    proc = _run_snippet(
        _BILLING_ENV,
        "import frontends.web_app as w\n"
        "c = w.app.test_client()\n"
        "c.post('/signup', data={'username':'owner','password':'Sufficiently-long-pw-1','password2':'Sufficiently-long-pw-1'})\n"
        "uid = w._auth_backend.lookup_by_username('owner').id\n"
        "w._auth_backend.set_stripe_customer(uid, 'cus_1')\n"
        "seen = {}\n"
        "w._billing.cancel_subscriptions = lambda cid: (seen.update(cid=cid) or 1)\n"
        "r = c.post('/account/delete')\n"
        "assert r.status_code == 200, r.status_code\n"
        "assert seen.get('cid') == 'cus_1', seen\n"
        "print('OK')\n",
    )
    assert proc.returncode == 0, proc.stderr + proc.stdout
    assert "OK" in proc.stdout


def test_change_plan_route_validates_and_reconciles():
    """A valid tier switches the plan and reconciles immediately; an unknown
    tier is rejected before any Stripe call."""
    proc = _run_snippet(
        _BILLING_ENV,
        "import frontends.web_app as w\n"
        "c = w.app.test_client()\n"
        "c.post('/signup', data={'username':'owner','password':'Sufficiently-long-pw-1','password2':'Sufficiently-long-pw-1'})\n"
        "uid = w._auth_backend.lookup_by_username('owner').id\n"
        "w._auth_backend.set_stripe_customer(uid, 'cus_1')\n"
        "seen = {}\n"
        "w._billing.change_plan = lambda cid, tier: seen.update(changed=(cid, tier))\n"
        "w._billing.reconcile_customer = lambda ab, cid: seen.update(reconciled=cid)\n"
        "r = c.post('/billing/change-plan', json={'tier':'power'})\n"
        "assert r.status_code == 200, r.status_code\n"
        "assert seen.get('changed') == ('cus_1','power'), seen\n"
        "assert seen.get('reconciled') == 'cus_1', seen\n"
        "seen.clear()\n"
        "r = c.post('/billing/change-plan', json={'tier':'bogus'})\n"
        "assert r.status_code == 400, r.status_code\n"
        "assert 'changed' not in seen, seen\n"
        "print('OK')\n",
    )
    assert proc.returncode == 0, proc.stderr + proc.stdout
    assert "OK" in proc.stdout


def test_cancel_and_resume_routes_reconcile():
    proc = _run_snippet(
        _BILLING_ENV,
        "import frontends.web_app as w\n"
        "c = w.app.test_client()\n"
        "c.post('/signup', data={'username':'owner','password':'Sufficiently-long-pw-1','password2':'Sufficiently-long-pw-1'})\n"
        "uid = w._auth_backend.lookup_by_username('owner').id\n"
        "w._auth_backend.set_stripe_customer(uid, 'cus_1')\n"
        "seen = {}\n"
        "w._billing.cancel_subscriptions = lambda cid: seen.update(cancelled=cid) or 1\n"
        "w._billing.resume_subscriptions = lambda cid: seen.update(resumed=cid) or 1\n"
        "w._billing.reconcile_customer = lambda ab, cid: seen.setdefault('reconciled', []).append(cid)\n"
        "assert c.post('/billing/cancel', json={}).status_code == 200\n"
        "assert c.post('/billing/resume', json={}).status_code == 200\n"
        "assert seen.get('cancelled') == 'cus_1', seen\n"
        "assert seen.get('resumed') == 'cus_1', seen\n"
        "assert seen.get('reconciled') == ['cus_1','cus_1'], seen\n"
        "print('OK')\n",
    )
    assert proc.returncode == 0, proc.stderr + proc.stdout
    assert "OK" in proc.stdout


def test_update_card_confirm_rejects_unconfirmed():
    """A forged/unconfirmed SetupIntent (ValueError from update_payment_method)
    is a 400, never a silent default-card swap."""
    proc = _run_snippet(
        _BILLING_ENV,
        "import frontends.web_app as w\n"
        "c = w.app.test_client()\n"
        "c.post('/signup', data={'username':'owner','password':'Sufficiently-long-pw-1','password2':'Sufficiently-long-pw-1'})\n"
        "uid = w._auth_backend.lookup_by_username('owner').id\n"
        "w._auth_backend.set_stripe_customer(uid, 'cus_1')\n"
        "def boom(cid, si):\n"
        "    raise ValueError('card has not been confirmed yet')\n"
        "w._billing.update_payment_method = boom\n"
        "r = c.post('/billing/update-card/confirm', json={'setup_intent_id':'seti_x'})\n"
        "assert r.status_code == 400, r.status_code\n"
        "print('OK')\n",
    )
    assert proc.returncode == 0, proc.stderr + proc.stdout
    assert "OK" in proc.stdout


def test_webhook_deleted_uses_embedded_object():
    """subscription.deleted must reconcile the embedded object (a retrieve would
    404 on a deleted sub), not re-fetch."""
    proc = _run_snippet(
        _BILLING_ENV,
        "import frontends.web_app as w\n"
        "seen = {}\n"
        "w._billing.construct_event = lambda p, s: "
        "{'type':'customer.subscription.deleted',"
        "'data':{'object':{'id':'sub_9','status':'canceled','customer':'cus_x'}}}\n"
        "def no_retrieve(i):\n"
        "    seen['retrieved'] = True\n"
        "    raise AssertionError('should not refetch a deleted sub')\n"
        "w._billing.retrieve_subscription = no_retrieve\n"
        "w._billing.reconcile_subscription = lambda ab, sub: seen.setdefault('id', sub['id'])\n"
        "c = w.app.test_client()\n"
        "r = c.post('/billing/webhook', data=b'{}', headers={'Stripe-Signature':'x'})\n"
        "assert r.status_code == 200, r.status_code\n"
        "assert seen.get('id') == 'sub_9', seen\n"
        "assert 'retrieved' not in seen, seen\n"
        "print('OK')\n",
    )
    assert proc.returncode == 0, proc.stderr + proc.stdout
    assert "OK" in proc.stdout
