# Billing (Stripe)

How Aime charges for access when `AIME_ACCESS_MODE=billing`. This is the
payment layer on top of the two columns Aime already gates on — `api_access`
(the send gate) and `tier` (the daily cost allowance). It is **off** in
`open`/`keys` mode; nothing here runs and no Stripe calls are made.

We never touch card data. Every card field is rendered by Stripe — the inline
**Payment Element** (mounted in Aime's own Billing tab) for subscribing *and*
for updating a card. The common management actions (switch plan, update card,
cancel, resume) all run **inline in the Billing tab**; only the long tail —
invoices, receipts, tax IDs — opens the hosted **Customer Portal** (the "More
options" link), because Stripe serves the Portal with frame-busting headers and
won't let it be embedded. Access is only ever granted
off a **server-side live read of Stripe** — never anything the client asserts —
so a spoofed return can't unlock an account. The signed **webhook** is the
standing authority (it reconciles every renewal/cancellation/revocation), and
the subscribe-confirm route reconciles once immediately off the same live read
so the trial unlocks the chat at once instead of waiting on (or hanging behind a
misconfigured) webhook. Both paths funnel through the same idempotent
`reconcile_subscription` seam, so they can't disagree.

## The model

- A **tier is a subscription plan.** Each tier (`light`, `power`) maps to a
  Stripe **Price** — the monthly amount the customer pays. That Price is
  *distinct* from the tier's daily cost cap (`AIME_TIER_*`), which is Aime's
  internal Anthropic-spend budget. The customer-facing price lives in Stripe;
  the cost cap lives in config.
- **Card at signup, then a 30-day free trial.** A new billing-mode account is
  created with `api_access=0` (no send access), exactly like a `keys`-mode
  account before it redeems a key. The user opens the **Billing** tab, starts a
  trial (always on the **default tier** — there's no plan picker at signup; see
  *Subscribing*), and enters a card in the inline Payment Element (card required
  up front even though the trial is free — see *Subscribing* below for why this
  is a two-step flow). The subscription goes `trialing`, which flips `api_access=1` —
  granted immediately by the confirm route's reconcile and re-confirmed by the
  `customer.subscription.created` webhook. After 30
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
   Switch plan ──► POST /billing/change-plan[/confirm] ──► SetupIntent → default PM
                                                          + Subscription.modify (prorated)
   Update card ──► POST /billing/update-card[/confirm] ──► SetupIntent → default PM
   Cancel / resume ─► POST /billing/{cancel,resume} ──► cancel_at_period_end on/off
        (all four reconcile immediately off a live read; the webhook re-confirms)
   Invoices / receipts / tax ─► POST /billing/portal ─► Stripe Customer Portal (hosted)
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
because `reconcile` is idempotent, retries are safe.

## Reliability: `api_access` is a self-healing cache

The webhook and the confirm-time reconcile keep `api_access` in step with Stripe
on the happy path, but **neither is load-bearing for correctness**. They are an
event pipeline, and every link can fail in the field — a webhook endpoint that
was never registered, a `whsec_` that drifted out of sync (signature → `400`,
silently dropped), an event Stripe delivers late or never, a confirm-time API
blip. Any one of those used to strand a paying user with `api_access=0` and no
recovery but an admin. That is the class of bug this section closes.

So the send gate treats `api_access` as a **cache of Stripe's truth, not the
truth itself.** Each user row carries `billing_synced_at` — the last time their
access was derived from a *live* Stripe read. When the gate (and `/me`) sees a
value older than a status-dependent TTL, it re-derives access from Stripe before
trusting it (`billing.access_is_stale` → `billing.refresh_access`, which funnels
through the same `reconcile_subscription` seam):

| Cached state | Re-check cadence | Why |
|--------------|------------------|-----|
| denied (`api_access=0`) | every `ACCESS_RECHECK_DENIED_SECONDS` (60s) | a paying user must never stay locked out, so re-check aggressively |
| granted (`api_access=1`) | every `ACCESS_RECHECK_GRANTED_SECONDS` (12h) | only hygiene (catch a cancellation whose webhook was lost), so re-check lazily |
| comped, or no Stripe customer | never | nothing to learn from Stripe |

This makes the guarantee **"up to date and paying ⇒ has access"** hold no matter
which event-delivery step failed: a stranded paying user is unlocked on their
next `/send` or `/me` (within 60s), automatically. The TTL bounds the cost — the
steady state is ~zero extra Stripe calls (a granted user is re-checked twice a
day; a never-paying one at most once a minute *while actively trying to send*).

`refresh_access` is **fail-safe**: on any Stripe error the cached state is left
exactly as-is (a transient outage must never lock out a paying user nor grant a
non-paying one) and `billing_synced_at` is left unstamped so the next request
retries. The reconcile is idempotent, so the lazy path, the confirm path, and
the webhook can all fire for the same user without conflicting.

The net layering, fastest to last-resort: **confirm-time reconcile** (unlocks
the instant the trial starts) → **webhook** (near-real-time grant/revoke on
every change) → **lazy gate reconcile** (the safety net that makes a lost event
self-correct). The security boundary is unchanged throughout: access is only
ever set from a server-side live read of Stripe, never anything the client
asserts.

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
- **One trial per customer.** The 30-day trial is granted only if the account is
  **trial-eligible** — both of: *none* of the customer's Stripe subscriptions has
  ever carried a trial, **and** the account's persisted `trial_used` flag is
  unset. A user who starts a trial, cancels, and subscribes again is charged
  immediately (no second trial). This blocks trial farming **on the same
  account**; the cross-account vector (a new email + a new card) can only be seen
  by Stripe — close it with a **Radar rule** (below), which the card-at-signup
  requirement makes possible.

  The `trial_used` flag (on the user row) is the local override that the Stripe
  check can't express: a **beta tester** who used the app for months in
  `open`/`keys` mode has *no* Stripe subscription, so Stripe sees no prior trial
  and would hand them a fresh 30 days at cutover. Flagging them `trial_used`
  denies that. Mechanics:
  - **Migration backfill.** When the `trial_used` column is first added, every
    *pre-existing* account is set to `1`; the column defaults `0`, so only
    accounts created *after* the migration are trial-eligible. So the moment a
    deployment ships this, its existing users subscribe with no fresh trial while
    new signups still get one — usually no admin action needed.
  - **Admin control.** `scripts/access_keys.py deny-trial <user>` /
    `allow-trial <user>` (and `deny-trial --all` for the cutover bulk), or the
    dashboard **Accounts** tab's per-row *Deny/Allow free trial* button and the
    *Deny free trial to everyone* bulk. The dashboard shows a **no trial** chip
    on flagged accounts.
  - When a trial *is* granted at confirm time, the account is stamped
    `trial_used` immediately, so the flag (not just Stripe) reflects it
    everywhere afterward (`/me`, a later resubscribe).

**There is no plan picker at signup.** Every trial starts on the **default tier**
(`USAGE_DEFAULT_TIER`, normally `light`), forced server-side in
`/billing/subscribe` — the request body's tier, if any, is ignored. This caps the
unpaid trial's cost exposure at the cheapest tier (its daily cap is Aime's
Anthropic-spend budget, so a free trial on the pricier tier would cost ~2× with
no revenue) and keeps the expensive tier off the trial-farming path. A user who
wants a bigger plan uses the inline **Change plan** control once subscribed —
free while trialing, prorated after. The forced tier rides the SetupIntent
metadata into step 2 and is read back server-side (never from the step-2 body),
so it can't be swapped after the card is entered.

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
   `billingPortal.sessions.create` works; test mode has a default. Aime now
   drives plan switching, card update, and cancel/resume **inline** (its own
   routes), so the Portal is only the "More options" fallback for invoices /
   receipts / tax — but leave **plan switching** enabled there too as a backstop
   (the webhook reconciles a Portal-side Price change the same way). Set the
   branding (logo, colors) there — the hosted Portal inherits it. (The inline
   Payment Element is themed separately, from Aime's own CSS variables via
   Stripe's Appearance API — see the Billing tab JS in `web_chat.html`.)
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

The companion is the **trial** cutover: those same long-time accounts would each
be handed a fresh 30-day trial on their first subscribe (Stripe has never seen a
trial for them). The `trial_used` migration backfill handles this automatically
(every account that existed when the column was added is marked ineligible), but
if you need to (re)assert it explicitly — e.g. accounts created in a window where
the column already existed — run `scripts/access_keys.py deny-trial --all` (or
the dashboard's *Deny free trial to everyone*). New signups stay eligible. So the
full cutover is two commands: `revoke-all` (zero send access) + `deny-trial
--all` (no free trials for existing accounts).

## Switching tiers / plans

A tier *is* the Stripe plan, so changing tier means changing the subscription's
Price. There are two paths, by who is driving:

**Prices are shown live, never duplicated in config.** The customer-facing amount
lives only in the Stripe Price; `billing.tier_prices()` reads it back (memoized an
hour — Prices are near-static) and `live_summary` folds it into the Billing tab
payload (`prices` + `default_tier`). The tab then labels the trial CTA, the plan
picker, and the current-plan line with the real amount, so editing a Price in the
Stripe Dashboard updates the UI with no redeploy. A tier whose Price can't be read
just falls back to its bare name. The "Change plan" panel also spells out the
proration in plain words: switching mid-cycle isn't charged immediately — the
difference lands on the next invoice (credit for unused time on the old plan,
charge for the rest of the period on the new one), so **switching up then back
before the next invoice only bills for the time actually spent on each plan**, and
during a trial nothing is charged at all.

A UI note: while a Stripe Payment Element (an iframe) is mounted, the Billing tab
drops the settings backdrop's live `backdrop-filter` blur (`#settings-backdrop.no-blur`).
The iframe composites over that blur, so without this every keystroke forced the
whole backdrop to re-blur on the main thread — which visibly stuttered typing into
the card field.

- **A paying user** changes their own plan **inline** in the Billing tab
  ("Change plan"), in **two steps like subscribe** — pick the plan, then confirm
  a card. This is deliberate: a plan switch moves real money, so it gets the
  same weight as signing up rather than a bare button, and all three
  money-moving actions (subscribe, change plan, update card) share one shape.
  Step 1 (`/billing/change-plan`) only mints a SetupIntent with the chosen tier
  in its metadata — nothing moves yet. Step 2 (`/billing/change-plan/confirm`)
  calls `billing.change_plan_with_card`, which reads the tier back **off
  Stripe's copy of the intent** (never the request body), makes the confirmed
  card the default on both the customer and the subscription — so the switch
  doubles as an update-card and can never land on a stale card — and then calls
  `billing.change_plan` (`Subscription.modify` swapping the item's Price,
  `proration_behavior="create_prorations"` — Stripe handles the proration; during
  a trial nothing is charged). The route then reconciles immediately so the new
  tier shows at once. The change also fires `customer.subscription.updated`, which the
  webhook reconciles the same way — new Price → new tier — so the inline path and
  the webhook can't disagree. (A Portal-side switch via "More options" works
  identically.) The new tier is always read off the live Price server-side, never
  from the request body.
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

## View-only access (skip billing)

A user does **not** have to subscribe to use Aime as a reader. `api_access` gates
only the cost-incurring routes — `/send`, `/upload`, and the agent/schedule
*run* endpoints (`api_access_required`). Login, browsing one's own topics and
conversations, and **viewing anything shared to the account by others** are
behind `login_required` only, so an account with no subscription already lands in
the app and can view everything shared with it; just the composer is locked.

The Billing tab makes this explicit rather than leaving a silently-disabled
composer: alongside *Start your free trial* it offers **Continue without
subscribing**, with copy that browsing and viewing shared content are free and a
plan is only needed to chat. The button just closes settings (there's no wall to
dismiss). The locked composer's placeholder reads *"View-only mode — start a free
trial …"* (or *"… subscribe …"* for a trial-ineligible account, keyed off
`/me`'s `billing.trial_eligible`). There is no separate "view-only" account
state — it's simply `api_access=0`, which the send gate already handles.

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
  `set_stripe_customer`, `set_subscription`, the `comp_access` flag +
  `set_comp_access` (sets comp + `api_access` together), and the `trial_used`
  flag (migration backfills pre-existing rows to 1) + `set_trial_used` /
  `set_trial_used_by_username` / `mark_all_trial_used`.
- `src/frontends/web_app.py` — `_billing_armed()`, the fail-closed startup
  check, the `/billing/{subscribe,subscribe/confirm,update-card,
  update-card/confirm,change-plan,change-plan/confirm,cancel,resume,portal,
  summary,webhook}` routes
  (`subscribe/confirm` enforces one-subscription / one-trial *and* honors
  `trial_used`; the inline-manage routes reconcile immediately via
  `_billing_reconcile_quiet`; the webhook 500s on unexpected errors so Stripe
  retries), the `/me` billing block (incl. `trial_eligible`), the
  `api_access_required` gate that leaves view-only access open, cancel-on-delete,
  and the recovery resume+reconcile.
- `src/aime/billing.py` helpers — `create_setup_intent` /
  `saved_payment_method` / `create_subscription` (the two-step inline subscribe),
  `create_card_update_intent` / `update_payment_method` (inline card swap),
  `create_plan_change_intent` / `change_plan_with_card` (the two-step
  card-backed plan switch) over `change_plan` (the bare prorated tier swap,
  also used by the webhook-agnostic paths), `subscription_state` (the
  subscribe guards), `cancel_subscriptions` / `resume_subscriptions` (inline
  cancel/resume *and* account delete/recover), `_current_live_subscription`.
- `resources/style/web_chat.html` — the Billing settings tab (incl. the inline
  Payment Element + its Appearance theming), the trial-vs-subscribe copy
  (`applyTrialCopy`, off `/me`'s `trial_eligible`), the **Continue without
  subscribing** (view-only) affordance, and the billing-mode composer-lock copy.
- `src/frontends/usage_dashboard.py` — the read-only Billing tab, and the
  Accounts-tab **Grant/Remove full access** (comp) control + `/accounts/comp`
  route, the **Deny/Allow free trial** per-row control + `/accounts/trial` route,
  and the **Deny free trial to everyone** bulk + `/accounts/deny-trial-all`
  (billing mode).
- `scripts/access_keys.py` — admin CLI: `deny-trial [--all] [<user>]` /
  `allow-trial <user>` (the trial-eligibility cutover + per-user override),
  alongside `revoke-all`.
