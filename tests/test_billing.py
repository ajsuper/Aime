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
                 "cancel_at_period_end": True}


def test_live_summary_no_subscription(monkeypatch, prices):
    monkeypatch.setattr(billing, "_initialized", True)
    monkeypatch.setattr(billing.stripe.Subscription, "list",
                        lambda **kw: _FakeList([]))
    s = billing.live_summary("cus_1")
    assert s["has_subscription"] is False


def test_reconcile_customer_uses_latest(monkeypatch, backend, prices, billing_user):
    monkeypatch.setattr(billing, "_initialized", True)
    sub = _sub(status="active", price="price_power", customer="cus_1")
    monkeypatch.setattr(billing.stripe.Subscription, "list",
                        lambda **kw: _FakeList([sub]))
    assert billing.reconcile_customer(backend, "cus_1") is True
    assert backend.lookup(billing_user.id).api_access is True


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
