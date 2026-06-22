"""Stripe billing for Aime (AIME_ACCESS_MODE=billing only).

This is the deferred "billing system" the access-control and usage-limit docs
point at: a thin wrapper around Stripe's hosted Checkout + Customer Portal, plus
the one seam that matters — ``reconcile_subscription`` — which maps a Stripe
subscription's live state back onto the two columns Aime already gates on:

* ``api_access`` (the send gate) — granted while the subscription is *trialing*
  or *active*, revoked when it is *past_due*/*canceled*/*unpaid*/etc.
* ``tier`` (the daily cost allowance) — derived from the subscription's Price.

We never touch card data: Stripe hosts every card field. A signed webhook is the
*only* authority that grants access (the Checkout success redirect grants
nothing), so a spoofed return URL can't unlock an account.

Design notes:

* **No Flask imports.** Mirrors ``aime.quota`` / ``aime.pricing`` — this module
  knows about Stripe and an ``AuthBackend``, nothing about HTTP.
* **No import of ``aime.auth``.** Callers pass the backend in, so ``auth`` never
  depends on ``billing`` and vice-versa.
* **The Price → tier map is the trust boundary.** The webhook reads the tier off
  the *actual* subscription Price (server-side), never off anything the client
  said, so a user can't select a tier they didn't pay for.
* **API version is pinned** (``stripe.api_version``) so a future Stripe upgrade
  can't silently reshape the webhook payloads under us.
"""

from __future__ import annotations

import logging
from typing import Any

import stripe

from . import config

_log = logging.getLogger("aime.billing")

# Pin the Stripe API version so payload shapes (and the subscription field
# layout this module reads) stay fixed regardless of the installed SDK's
# default. Matches the SDK pinned in requirements.txt; bump both together.
STRIPE_API_VERSION = "2026-05-27.dahlia"

# Subscription statuses → whether the user may send. None means "leave
# api_access untouched" (e.g. 'incomplete': the first payment hasn't resolved
# yet, so neither grant nor revoke).
_ACCESS_GRANTING = frozenset({"trialing", "active"})
_ACCESS_REVOKING = frozenset({
    "past_due", "canceled", "unpaid", "incomplete_expired", "paused",
})

_initialized = False


def billing_enabled() -> bool:
    """True when Stripe is fully configured (secret + webhook secret + a Price
    per tier). The access-mode check is the caller's job: the web layer's
    ``_billing_armed()`` is ``AIME_ACCESS_MODE == 'billing' and
    billing_enabled()``. The startup check refuses to launch billing mode unless
    this is True (otherwise the send gate is armed with no way to gain
    access)."""
    return config.stripe_configured()


def init_stripe() -> None:
    """Configure the Stripe SDK once (api key + pinned api version). Safe to
    call repeatedly; a no-op when the secret key isn't set."""
    global _initialized
    if _initialized or not config.STRIPE_SECRET_KEY:
        return
    stripe.api_key = config.STRIPE_SECRET_KEY
    stripe.api_version = STRIPE_API_VERSION
    _initialized = True


# ---------------------------------------------------------------------------
# Field access helpers — Stripe objects support both attribute and item access,
# but we read defensively so a missing/renamed field degrades to None instead
# of raising inside a webhook.
# ---------------------------------------------------------------------------

def _get(obj: Any, key: str, default: Any = None) -> Any:
    if obj is None:
        return default
    try:
        return obj[key]
    except (KeyError, TypeError):
        return getattr(obj, key, default)


def _first_item(subscription: Any) -> Any:
    items = _get(subscription, "items")
    data = _get(items, "data") if items is not None else None
    if data:
        return data[0]
    return None


def _price_id(subscription: Any) -> str | None:
    item = _first_item(subscription)
    price = _get(item, "price")
    if price is None:
        return None
    # price may be a full object or a bare id string.
    if isinstance(price, str):
        return price
    return _get(price, "id")


def _current_period_end(subscription: Any) -> int | None:
    """Renewal timestamp. As of recent Stripe API versions this lives on the
    subscription *item*, not the subscription; fall back to the legacy
    top-level field for older versions."""
    item = _first_item(subscription)
    end = _get(item, "current_period_end")
    if end is None:
        end = _get(subscription, "current_period_end")
    return end


# ---------------------------------------------------------------------------
# Stripe session creation (called from the web routes)
# ---------------------------------------------------------------------------

def ensure_customer(auth_backend, user) -> str:
    """Return the user's Stripe Customer id, creating + persisting it on first
    use. Persisted *before* Checkout opens so the webhook can always resolve a
    subscription event back to this user via lookup_by_stripe_customer."""
    init_stripe()
    if user.stripe_customer_id:
        return user.stripe_customer_id
    customer = stripe.Customer.create(
        email=user.email or None,
        metadata={"aime_user_id": str(user.id), "username": user.username},
    )
    auth_backend.set_stripe_customer(user.id, customer.id)
    return customer.id


def create_checkout_session(
    *, user_id: int, customer_id: str, tier: str,
    success_url: str, cancel_url: str, trial_days: int = 0,
) -> str:
    """Create a subscription Checkout Session for ``tier`` and return its hosted
    URL. Card is collected up front (``payment_method_collection='always'``).
    ``trial_days`` > 0 grants a free trial; pass 0 for a returning customer who
    already used their trial (they're charged immediately — the anti-abuse gate
    lives in the caller). The user id is stamped on both the session
    (client_reference_id) and the subscription (metadata) as the webhook's
    fallback identity."""
    init_stripe()
    price_id = config.stripe_price_for_tier(tier)
    if not price_id:
        raise ValueError(f"no Stripe price configured for tier {tier!r}")
    sub_data: dict = {"metadata": {"aime_user_id": str(user_id)}}
    if trial_days and trial_days > 0:
        sub_data["trial_period_days"] = trial_days
    session = stripe.checkout.Session.create(
        mode="subscription",
        customer=customer_id,
        line_items=[{"price": price_id, "quantity": 1}],
        subscription_data=sub_data,
        payment_method_collection="always",
        client_reference_id=str(user_id),
        success_url=success_url,
        cancel_url=cancel_url,
    )
    return session.url


def create_portal_session(*, customer_id: str, return_url: str) -> str:
    """Create a Stripe Customer Portal session and return its hosted URL. The
    Portal is where the user updates their card, switches plan, or cancels."""
    init_stripe()
    session = stripe.billing_portal.Session.create(
        customer=customer_id, return_url=return_url,
    )
    return session.url


# Statuses that count as "the customer is currently subscribed" — used to guard
# against a second subscription and to decide what to cancel/resume. Excludes
# canceled/incomplete_expired (terminal) and incomplete (never started).
_LIVE_STATUSES = frozenset({"trialing", "active", "past_due", "unpaid", "paused"})


def _list_subscriptions(customer_id: str, limit: int = 100) -> list:
    init_stripe()
    subs = stripe.Subscription.list(
        customer=customer_id, status="all", limit=limit,
    )
    return _get(subs, "data") or []


def subscription_state(customer_id: str) -> dict:
    """One Stripe round-trip answering the two questions Checkout needs:
    ``has_active`` (is the customer already subscribed — block a second
    subscription) and ``used_trial`` (has any of their subscriptions ever
    carried a trial — don't grant another). Used by /billing/checkout."""
    subs = _list_subscriptions(customer_id)
    return {
        "has_active": any(_get(s, "status") in _LIVE_STATUSES for s in subs),
        "used_trial": any(_get(s, "trial_end") is not None for s in subs),
    }


def cancel_subscriptions(customer_id: str) -> int:
    """Schedule cancellation of every live subscription at its period end
    (``cancel_at_period_end``). Used when a user deletes their account — they
    stop being billed, but keep the access they already paid for through the
    grace period, and the cancellation is reversible if they recover (see
    resume_subscriptions). Returns how many were scheduled."""
    init_stripe()
    n = 0
    for s in _list_subscriptions(customer_id):
        if _get(s, "status") in _LIVE_STATUSES and not _get(s, "cancel_at_period_end"):
            stripe.Subscription.modify(_get(s, "id"), cancel_at_period_end=True)
            n += 1
    return n


def resume_subscriptions(customer_id: str) -> int:
    """Undo a pending cancel_at_period_end on a customer's live subscriptions —
    the inverse of cancel_subscriptions, used when a soft-deleted account is
    recovered within the grace period. Returns how many were resumed."""
    init_stripe()
    n = 0
    for s in _list_subscriptions(customer_id):
        if _get(s, "status") in _LIVE_STATUSES and _get(s, "cancel_at_period_end"):
            stripe.Subscription.modify(_get(s, "id"), cancel_at_period_end=False)
            n += 1
    return n


def live_summary(customer_id: str) -> dict:
    """A live read of the customer's current subscription for the Billing tab.
    Heavier than the persisted columns on /me, so this is only called when the
    tab is opened. Returns plan/status/renewal detail; ``has_subscription`` is
    False when the customer has never subscribed."""
    init_stripe()
    subs = stripe.Subscription.list(customer=customer_id, status="all", limit=1)
    data = _get(subs, "data") or []
    if not data:
        return {"has_subscription": False, "status": None, "tier": None,
                "trial_end": None, "current_period_end": None,
                "cancel_at_period_end": False}
    sub = data[0]
    return {
        "has_subscription": True,
        "status": _get(sub, "status"),
        "tier": config.tier_for_stripe_price(_price_id(sub)),
        "trial_end": _get(sub, "trial_end"),
        "current_period_end": _current_period_end(sub),
        "cancel_at_period_end": bool(_get(sub, "cancel_at_period_end")),
    }


# ---------------------------------------------------------------------------
# Webhook plumbing
# ---------------------------------------------------------------------------

def construct_event(payload: bytes, sig_header: str | None):
    """Verify a webhook payload's Stripe signature and return the event. Raises
    ``stripe.error.SignatureVerificationError`` / ``ValueError`` on a bad
    signature or malformed body — the route turns those into a 400."""
    init_stripe()
    return stripe.Webhook.construct_event(
        payload, sig_header, config.STRIPE_WEBHOOK_SECRET,
    )


def retrieve_subscription(subscription_id: str):
    """Fetch a subscription fresh from Stripe. The webhook re-fetches rather
    than trusting the (possibly out-of-order) event body, so a stale event can
    never overwrite newer state."""
    init_stripe()
    return stripe.Subscription.retrieve(subscription_id)


def _resolve_user(auth_backend, subscription):
    """Find the Aime user a subscription belongs to. Primary key is the Stripe
    Customer id (persisted before checkout); the subscription metadata
    aime_user_id is a belt-and-suspenders fallback."""
    customer_id = _get(subscription, "customer")
    if isinstance(customer_id, dict) or not isinstance(customer_id, (str, type(None))):
        customer_id = _get(customer_id, "id")
    if customer_id:
        user = auth_backend.lookup_by_stripe_customer(customer_id)
        if user is not None:
            return user
    meta = _get(subscription, "metadata") or {}
    raw_id = _get(meta, "aime_user_id")
    if raw_id:
        try:
            return auth_backend.lookup(int(raw_id))
        except (ValueError, TypeError):
            return None
    return None


def reconcile_customer(auth_backend, customer_id: str) -> bool:
    """Reconcile a customer's *current* subscription (its latest one) against
    Aime's state. Used outside the webhook — e.g. restoring access on account
    recovery — where we have a customer id but no event. Returns True when a
    subscription was found and reconciled."""
    init_stripe()
    subs = stripe.Subscription.list(customer=customer_id, status="all", limit=1)
    data = _get(subs, "data") or []
    if not data:
        return False
    return reconcile_subscription(auth_backend, data[0])


def reconcile_subscription(auth_backend, subscription) -> bool:
    """The core seam: map a subscription's live state onto api_access + tier +
    the persisted subscription snapshot. Idempotent (pure state-set), so it is
    safe to call for every (at-least-once, possibly-reordered) webhook. Returns
    True when a user was found and updated, False otherwise (logged)."""
    user = _resolve_user(auth_backend, subscription)
    if user is None:
        _log.warning("billing: no user for subscription %s (customer %s)",
                     _get(subscription, "id"), _get(subscription, "customer"))
        return False

    # Complimentary access wins over Stripe: an admin has put this user on the
    # house, so a lapsed/absent subscription must never revoke them and their
    # tier is the admin's choice, not the Price. Leave api_access + tier alone.
    if user.comp_access:
        _log.info("billing: skipping reconcile for comped user %s (sub %s)",
                  user.id, _get(subscription, "id"))
        return True

    status = _get(subscription, "status")

    # Access: grant on trialing/active, revoke on the dunning/terminal states,
    # leave untouched for ambiguous ones (e.g. 'incomplete').
    if status in _ACCESS_GRANTING:
        auth_backend.set_api_access(user.id, True)
    elif status in _ACCESS_REVOKING:
        auth_backend.set_api_access(user.id, False)

    # Tier: only change it when the Price maps to a known tier. An unknown Price
    # (e.g. a plan we haven't wired up) leaves the existing tier in place rather
    # than guessing.
    tier = config.tier_for_stripe_price(_price_id(subscription))
    if tier is not None:
        auth_backend.set_tier(user.id, tier)
    else:
        _log.warning("billing: subscription %s has unmapped price %s",
                     _get(subscription, "id"), _price_id(subscription))

    auth_backend.set_subscription(
        user.id,
        subscription_id=_get(subscription, "id"),
        status=status,
        trial_end=_get(subscription, "trial_end"),
    )
    return True
