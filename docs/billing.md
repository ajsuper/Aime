# Billing (Stripe)

How Aime charges for access when `AIME_ACCESS_MODE=billing`. This is the
payment layer on top of the two columns Aime already gates on — `api_access`
(the send gate) and `tier` (the daily cost allowance). It is **off** in
`open`/`keys` mode; nothing here runs and no Stripe calls are made.

We never touch card data. Every card field is rendered by Stripe — the inline
**Payment Element** (mounted in Aime's own Billing tab) for subscribing, and the
hosted **Customer Portal** for managing/cancelling. A signed **webhook** is the
*only* authority that grants access (creating a subscription or confirming a
card grants nothing on its own), so a spoofed return can't unlock an account.

## The model

- A **tier is a subscription plan.** Each tier (`light`, `power`) maps to a
  Stripe **Price** — the monthly amount the customer pays. That Price is
  *distinct* from the tier's daily cost cap (`AIME_TIER_*`), which is Aime's
  internal Anthropic-spend budget. The customer-facing price lives in Stripe;
  the cost cap lives in config.
- **Card at signup, then a 30-day free trial.** A new billing-mode account is
  created with `api_access=0` (no send access), exactly like a `keys`-mode
  account before it redeems a key. The user opens the **Billing** tab, starts a
  trial, and enters a card in the inline Payment Element (card required up front
  even though the trial is free — see *Subscribing* below for why this is a
  two-step flow). The webhook sees `trialing` and flips `api_access=1`. After 30
  days Stripe auto-charges; on success the subscription goes `active` and access
  continues. On cancellation or a failed payment the subscription goes
  `canceled`/`past_due`/`unpaid`, and the webhook flips `api_access=0`.
- **The webhook reads the tier off the *actual* subscription Price** (server
  side), never off anything the client said — so a user can't select a tier
  they didn't pay for.

## Flow

```
signup (billing mode)            account created, api_access=0
   └─ Billing tab "Start trial" ─► POST /billing/subscribe ──► SetupIntent (client secret)
                                          │
              inline Payment Element confirms the card (stripe.confirmSetup)
                                          │
        POST /billing/subscribe/confirm ─► create subscription on saved card (trial 30d)
                                                                   │
   Stripe ──────── customer.subscription.* ────────────────────────┤
                                                                   ▼
                                          POST /billing/webhook (signature-verified)
                                                   └─ reconcile: status→api_access, price→tier
   Manage / cancel / update card ─► POST /billing/portal ─► Stripe Customer Portal (hosted)
```

The subscribe flow is **two steps on purpose.** A free-trial subscription
created directly would enter `trialing` the instant it exists — i.e. *before*
the card is confirmed — granting access (and consuming the customer's one trial)
even if they abandoned the card form. So step 1 (`/billing/subscribe`) only
creates a **SetupIntent** to collect+save the card (no subscription yet, no
access, no trial spent); step 2 (`/billing/subscribe/confirm`) creates the
subscription on that saved card *after* it's confirmed, so it goes `trialing`
with a payment method already attached. The one-trial / no-double-subscription
guards run in step 2 (the request that actually creates the subscription). For a
returning customer who already used their trial, the first invoice is charged
immediately off-session (`error_if_incomplete`, so a decline surfaces as an
error instead of a stuck `incomplete` subscription).

Status → access mapping (`aime.billing.reconcile_subscription`):

| Subscription status | `api_access` |
|---------------------|--------------|
| `trialing`, `active` | granted (1) |
| `past_due`, `unpaid`, `canceled`, `incomplete_expired`, `paused` | revoked (0) |
| `incomplete` | left unchanged (first payment not resolved yet) |

The webhook **re-fetches** the subscription from Stripe on each event rather
than trusting the event body — Stripe delivers events at-least-once and not
strictly ordered, so trusting an embedded (possibly stale) `updated` could
overwrite newer state. `customer.subscription.deleted` is the exception (a
re-fetch would 404), so its embedded object is taken as the final state.

**Retry semantics.** A bad signature → `400` (never acted on). A well-formed
event for a user we can't resolve is **not** an error — `reconcile` returns
`False` and the webhook acks `200` (no retry loop). An *unexpected* failure (DB
locked, Stripe API blip) returns **`500`** so Stripe retries with backoff;
because `reconcile` is idempotent, retries are safe, and Stripe's retry is the
only safety net against a permanently-lost grant/revoke.

## Subscribing: one subscription, one trial

`POST /billing/subscribe/confirm` — the step that actually creates the
subscription — guards two things (one Stripe round-trip,
`billing.subscription_state`). `/billing/subscribe` (step 1) also pre-checks the
double-subscription guard so the card form isn't even shown to an already-paying
user, but step 2 is the authoritative gate:

- **No double subscription.** If the customer already has a live subscription
  (`trialing`/`active`/`past_due`/`unpaid`/`paused`) the route returns `409` and
  points the user at **Manage billing** instead — so a direct POST can't create
  a second, double-billing subscription behind the UI's back.
- **One trial per customer.** The 30-day trial is granted only if *none* of the
  customer's subscriptions has ever carried a trial. A user who starts a trial,
  cancels, and subscribes again is charged immediately (no second trial). This
  blocks trial farming **on the same account**; the cross-account vector (a new
  email + a new card) can only be seen by Stripe — close it with a **Radar rule**
  (below), which the card-at-signup requirement makes possible.

The chosen tier is carried on the SetupIntent metadata and read back server-side
in step 2 — never taken from the step-2 request body — so it can't be swapped
after the card is entered.

## Operator setup

1. **Create two Products/Prices** in the Stripe Dashboard — one recurring
   (monthly) Price per tier. Copy each Price ID (`price_…`).
2. **Set the environment** (see `.env.example`):
   - `AIME_ACCESS_MODE=billing`
   - `AIME_STRIPE_SECRET_KEY` (`sk_test_…` / `sk_live_…`)
   - `AIME_STRIPE_PUBLISHABLE_KEY` (`pk_test_…` / `pk_live_…`) — shipped to the
     browser for the inline Payment Element. Must be from the **same account and
     mode** as the secret key.
   - `AIME_STRIPE_WEBHOOK_SECRET` (`whsec_…`, from step 3)
   - `AIME_STRIPE_PRICE_LIGHT`, `AIME_STRIPE_PRICE_POWER`
   - `AIME_STRIPE_TRIAL_DAYS` (default 30)
   - `AIME_PUBLIC_BASE_URL` (absolute, no trailing slash; e.g.
     `https://aime.example.com`. `http://localhost:5000` is fine in test mode.)
     **Required** — the app refuses to start in billing mode without it, so the
     Stripe return URLs can't be forged from a spoofed `Host` header.

   The app **refuses to start** in billing mode unless the secret key,
   publishable key, webhook secret, and a Price for each tier are all set —
   otherwise the send gate would be armed with no way for anyone to gain access.
3. **Register the webhook endpoint** in Stripe at
   `https://<your-host>/billing/webhook`, subscribed to
   `customer.subscription.created`, `customer.subscription.updated`, and
   `customer.subscription.deleted`. Copy its signing secret into
   `AIME_STRIPE_WEBHOOK_SECRET`. (For local testing use
   `stripe listen --forward-to localhost:5000/billing/webhook`, which prints a
   `whsec_`.)
4. **Configure the Customer Portal** in the Stripe Dashboard. In **live** mode
   the Portal needs an explicit configuration saved before
   `billingPortal.sessions.create` works; test mode has a default. Enable
   **plan switching** between the two Prices (so users can upgrade/downgrade —
   the webhook reconciles the new Price → tier). Set the branding (logo, colors)
   there too — the hosted Portal inherits it. (The inline Payment Element is
   themed separately, from Aime's own CSS variables via Stripe's Appearance
   API — see the Billing tab JS in `web_chat.html`.)
5. **Limit free trials (anti-abuse).** Aime already blocks a *second* trial on
   the same Stripe customer, but a determined user can make a new account with a
   new email + card. Only Stripe can see the card, so close that vector with a
   **Radar rule** — e.g. *block a payment if the card fingerprint has previously
   started a trial* — or Stripe Billing's "limit one trial per customer" option.
   The card-at-signup requirement (`payment_method_collection="always"`) is what
   makes a card-fingerprint rule possible.

### Cutover caveat (important)

Users created during a prior `open`/`keys` period already hold `api_access=1`
and would be **silently grandfathered** into free access with no subscription.
When switching a live deployment to `billing`, run
`scripts/access_keys.py revoke-all` once so billing re-grants access only on a
real subscription. (This is the same sharp edge documented in
[access-control.md](access-control.md).)

## Switching tiers / plans

A tier *is* the Stripe plan, so changing tier means changing the subscription's
Price. There are two paths, by who is driving:

- **A paying user** changes their own plan in the **Stripe Customer Portal**
  ("Manage billing" in the Billing tab). Enable plan switching between the two
  Prices in the Portal configuration. The change fires
  `customer.subscription.updated`; the webhook reconciles the new Price → the new
  tier. (Stripe handles proration.)
- **An admin** sets a tier from the dashboard **Accounts** tab. For a **comped**
  or not-yet-subscribed user this is authoritative (no subscription to fight it).
  For a **paying** user a manual tier change is informational only — their next
  subscription event reconciles the tier back to whatever their Price maps to, so
  to actually move a payer between plans, change their subscription in Stripe.

## Complimentary access (comp)

To authorize someone **without payment** (yourself, a tester, a friend), use the
**Grant full access** button on the dashboard **Accounts** tab (shown in billing
mode). It sets a durable `comp_access` flag that:

- grants send access immediately (`api_access` is set alongside it), and
- makes the Stripe webhook **skip** that user entirely — `reconcile_subscription`
  returns early for a comped user, so a missing or canceled subscription can
  never revoke them, and their tier stays whatever the admin set (not Price-
  derived).

A comped user sees a "complimentary full access — no subscription needed" note
in their Billing tab instead of the trial CTA, and is never asked for a card.
**Remove full access** clears the flag and turns send access off (they can then
subscribe to regain it). Set their tier with the Accounts tier dropdown — it
sticks because the webhook leaves comped users alone.

The same `comp_access` toggle is also surfaced in **keys mode** (labeled
"always-allow + reset"), where it's just durable admin-granted send access — no
Stripe to skip. In both modes, **granting comp also resets the user's usage
budget to 100%** (`QuotaStore.reset_full`), so the button doubles as a per-user
refill; comp gates *send access*, not the daily budget, which still applies after
the reset (see [usage-limits.md](usage-limits.md)).

## Account deletion & billing

Deleting an account must not keep charging a departed user. When a user
soft-deletes their account (`POST /account/delete`), Aime schedules their Stripe
subscription to **cancel at period end** (`billing.cancel_subscriptions`):

- They keep the access they already paid for through the (reversible) grace
  period, and **no further charge** is taken.
- If they **recover** the account within the grace period, the pending
  cancellation is undone (`resume_subscriptions`) and access is reconciled from
  the live subscription — so recovery restores a paying user seamlessly.
- Because the plans are **monthly**, the period ends within the 30-day grace
  window, so the subscription is fully gone before the permanent purge. (A hypo­
  thetical annual plan would outlast the grace period — cancel it manually at
  purge, or switch the delete to cancel-immediately.)

The cancel is best-effort: a Stripe outage never blocks the deletion the user
asked for (it's logged). After the subscription cancels, its
`subscription.deleted` webhook arrives for an already-soft-deleted user, so
`reconcile` finds no account and no-ops — which is correct.

## Admin view

The admin dashboard's **Billing** tab (billing mode) lists each subscriber with
the subscription status the webhook last recorded, their live `api_access`, and
a deep link to the Stripe customer. It is **read-only** — Stripe is the system
of record. Plan/payment changes happen in Stripe or the user's own portal.

## Test-mode walkthrough

1. Set `AIME_ACCESS_MODE=billing`, the `AIME_STRIPE_*` **test** keys, two test
   Price IDs, and `AIME_PUBLIC_BASE_URL=http://localhost:5000`.
2. `stripe listen --forward-to localhost:5000/billing/webhook` (copy the
   `whsec_` into the env and restart).
3. Sign up → the composer is locked → open **Billing** → pick a plan → **Start
   30-day free trial** → the inline card panel appears → card
   `4242 4242 4242 4242` → **Start free trial**.
4. Confirm: the card is saved via the SetupIntent, `/billing/subscribe/confirm`
   creates the subscription, and the webhook fires
   `customer.subscription.created (trialing)`; `/me` shows
   `billing.status=trialing`; `api_access` flips true; the composer unlocks; the
   tier matches the chosen Price. (To exercise the 3-D Secure redirect path, use
   test card `4000 0027 6000 3184` — Stripe redirects to the return URL and the
   page finishes the subscription on the way back.)
5. In the Portal, cancel → `customer.subscription.deleted` → the composer
   re-locks; the dashboard shows `canceled`.
6. A failed renewal (Stripe retries → `past_due`/`unpaid`) fires
   `customer.subscription.updated`, which revokes access — the change rides the
   subscription-status event, not `invoice.payment_failed` directly.

## Implementation map

- `src/aime/billing.py` — the Stripe wrapper + the `reconcile_subscription`
  seam (status→`api_access`, Price→tier). No Flask, no `aime.auth` import.
- `src/aime/config.py` — `AIME_STRIPE_*` settings, `stripe_price_for_tier` /
  `tier_for_stripe_price`, `stripe_configured()`.
- `src/aime/auth.py` — the `stripe_customer_id` / `stripe_subscription_id` /
  `subscription_status` / `trial_end` columns, `lookup_by_stripe_customer`,
  `set_stripe_customer`, `set_subscription`, and the `comp_access` flag +
  `set_comp_access` (sets comp + `api_access` together).
- `src/frontends/web_app.py` — `_billing_armed()`, the fail-closed startup
  check, the `/billing/{subscribe,subscribe/confirm,portal,summary,webhook}`
  routes (`subscribe/confirm` enforces one-subscription / one-trial; the webhook
  500s on unexpected errors so Stripe retries), the `/me` billing block,
  cancel-on-delete, and the recovery resume+reconcile.
- `src/aime/billing.py` helpers — `create_setup_intent` /
  `saved_payment_method` / `create_subscription` (the two-step inline subscribe),
  `subscription_state` (the subscribe guards), `cancel_subscriptions` /
  `resume_subscriptions` (account delete/recover).
- `resources/style/web_chat.html` — the Billing settings tab (incl. the inline
  Payment Element + its Appearance theming) and the billing-mode composer-lock
  copy.
- `src/frontends/usage_dashboard.py` — the read-only Billing tab, and the
  Accounts-tab **Grant/Remove full access** (comp) control + `/accounts/comp`
  route (billing mode).
