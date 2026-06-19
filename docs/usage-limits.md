# Usage limits

How Aime caps each user's cost. This is the always-on budget that protects the
Anthropic spend — distinct from the opt-in usage *log* (`aime.usage`, for
reporting). It covers the token-bucket model, the two tiers, what is enforced
today (and what is deliberately deferred), how it's armed, and the admin
controls.

## The model: a banked daily allowance (token bucket)

Each user has a **balance** in USD and a tier-defined **daily allowance**. The
balance is a token bucket:

- It **refills continuously** at the tier's daily rate — there is no midnight
  reset cliff, capacity just trickles back at `allowance/day`.
- It **banks up to `USAGE_BANK_DAYS` (default 7) days'** worth. A quiet day's
  unused allowance carries forward to a busy one, up to that ceiling.
- Every API call's **real cost** is debited (priced by `aime.pricing`, the same
  model the usage report and dashboard use). Title/compaction/router/web-search
  calls all count — they are real spend.
- A fresh user starts **full** (at the ceiling).

This fixes the weakness of a hard daily cap: it neither wastes your quiet days
nor clips your busy ones, while still **guaranteeing a daily refill** (so you can
always use Aime each day) and bounding exposure (the ceiling stops anyone
hoarding a month and dumping it at once).

The user never sees dollars. The account meter leads with a **battery-style
percentage** — `pct_full`, the balance as a fraction of the full bank (the 7-day
ceiling), always 0–100% — with the banked days in parentheses (e.g. "68%
(≈ 4.8 days)"). The snapshot also carries `pct_of_day` (balance ÷ one day's
allowance, which reads above 100% when banked) for callers that want it.

## Tiers

Two tiers, each a daily USD allowance (configurable):

| Tier  | Daily allowance | Max banked (7 days) |
|-------|-----------------|---------------------|
| light | `$0.75` (`AIME_TIER_LIGHT`) | `$5.25` |
| power | `$1.50` (`AIME_TIER_POWER`) | `$10.50` |

Defaults sit ~1.4–1.5× above each tier's observed average daily cost, so a
normal day never trips the cap; only blow-out days draw the bank down. A new
account is stamped `AIME_USAGE_DEFAULT_TIER` (`light`). An admin moves users
between tiers; later, the billing system will.

## What is enforced

When a turn's debit crosses a threshold the user gets a calm, transient banner:

- **running low** — balance below `AIME_USAGE_NOTIFY_LOW_FRACTION` (0.25) of a
  day's allowance. Notify only; the turn proceeds.
- **out** — balance spent (≤ 0). Sending is **blocked.**

At an empty balance the budget is now a **hard stop**: `/send` refuses the turn
(HTTP `402`) with a calm "you've used up today's Aime — your access will be back
tomorrow" message, and the frontend locks the composer. Because the balance
refills continuously (no midnight reset), access returns on its own as the
allowance trickles back over the next day — the composer re-enables the moment
`/me` reports the budget is no longer over. The classification lives behind a
single seam, `aime.quota.enforcement_decision` (`ALLOW` / `NOTIFY_LOW` /
`OVER`); the `/send` route blocks on `OVER` and notifies on `NOTIFY_LOW`.

**The block is one turn behind (by design).** `/send` checks the budget
*before* the turn runs, but the cost is debited *during* it (each API call, in
`_record_usage`). So a user with any positive balance — even 1% — passes the
check and runs one more full turn, which is then debited in full and may push
the balance well negative. They are only refused on the *next* send. This means
the budget bounds **steady-state** spend, not the cost of a single turn: a long
context with many tool calls (or a web-search subagent) can overshoot the
remaining balance by one expensive turn. That is acceptable for the free-tester
cohort the limits target; a real per-turn ceiling would require a pre-turn cost
*estimate* gate, which is deliberately not built. The continuous refill absorbs
the overshoot — a turn that drives the balance to `−$0.40` simply delays the
return of access by that much allowance.

The debit itself **fails open**: any error in pricing or the quota store lets
the turn proceed uncharged (a cost-control bug must never break a turn). Because
a *persistent* failure would silently disable enforcement for everyone, the
debit path logs at `warning` (`provider_backend` logger) when it fails — that
log line is the signal that the ledger is broken.

## Background agents: limit, don't punish

Background agents (the headless-worker framework) draw the **same** budget as
chat — every agent turn and any offloaded web-search is debited from the user's
bucket (the runner takes the user's `QuotaMeter`; see
[background-agents](../src/aime/agents/runner.py)). So an agent can never be a
free, uncapped channel around the budget.

But the **block** is applied by *how the run was started*, not by what it costs.
The distinction is **new on-demand work** vs **automation the user already set
up**:

- **On-demand runs are blocked when over budget**, exactly like `/send` (same
  402): the ad-hoc agent launcher (`/agents/run`), the saved-agent **Run**
  button (`/agents/<id>/run`), and **run schedule now**
  (`/schedules/<id>/run`). Without this, an out-of-budget user could just spin
  up a one-off agent to keep working — a hole around the chat block. The seam is
  `web_app._user_over_budget`.
- **Recurring runs fired by the scheduler loop are *not* blocked.** A daily
  "tell me about my day" briefing the user scheduled keeps arriving even on a
  day they spent their chat budget. Blocking it would punish usage rather than
  limit it. The cost is still debited (so it draws the bucket down and is
  bounded over time), but the run proceeds. The autonomous path
  (`web_app._scheduler_run_agent`) deliberately skips the budget check.

The durable paid/unpaid line is **`api_access`**, not the daily budget: *every*
agent path (scheduled and on-demand) is gated on it, so when the (deferred)
billing webhook revokes access for a user who stops paying, both their recurring
and on-demand agents stop. The budget is a daily soft limit; `api_access` is the
hard switch.

## Arming: driven by `AIME_ACCESS_MODE`

Usage limits are **not** a separate flag. They arm exactly like the `/send`
`api_access` gate (see [access-control.md](access-control.md)):

| Mode      | Usage limits | Notes |
|-----------|--------------|-------|
| `open`    | **off**      | Trusted local/personal use — nothing is metered; no meter is attached, `quota.sql` is never written. |
| `keys`    | **on**       | The free-tester cohort. Tiers are how their cost is bounded. |
| `billing` | **on**       | A tier *is* the subscription plan. The (deferred) Stripe webhook sets `api_access` + `tier` together. |

## Config (environment)

| Variable | Default | Meaning |
|----------|---------|---------|
| `AIME_TIER_LIGHT` | `0.75` | Light daily allowance (USD). |
| `AIME_TIER_POWER` | `1.50` | Power daily allowance (USD). |
| `AIME_USAGE_BANK_DAYS` | `7` | Days of allowance the balance may bank (ceiling). |
| `AIME_USAGE_NOTIFY_LOW_FRACTION` | `0.25` | Fraction of a day below which "running low" fires. |
| `AIME_USAGE_DEFAULT_TIER` | `light` | Tier stamped on a new account. |

## Admin controls

- **Web dashboard** (`src/frontends/usage_dashboard.py`, password-gated):
  - **Accounts** tab — a **Tier** dropdown per user (instant change) and a
    **Usage** column showing each account's remaining allowance (% of full bank).
  - **Costs** tab — a **Tiers** section (tier-fit analytics): per tier, the user
    count, average daily spend, average **utilization** of the daily allowance,
    how many users went **over** it, and on what fraction of active user-days.
    Utilization over 100% (or a high over-rate) flags a tier that's undersized
    for its cohort. Computed from `usage.jsonl` joined with each user's current
    tier; requires `AIME_USAGE_LINK_USERS=1`.
  - **Billing** tab — placeholder documenting the current tiers and the Stripe
    drop-in point.
- **CLI** — `scripts/access_keys.py tier <username> <light|power>`, at parity
  with the dashboard.

## Implementation map

- `src/aime/pricing.py` — the cost model (prices, `api_cost`, `record_from_usage`,
  `cost_from_usage`). Single source of truth for the report, dashboard, and the
  live debit. `scripts/usage_report.py` re-exports it.
- `src/aime/quota.py` — `QuotaStore` (sqlite `quota.sql`), `QuotaMeter` (per-user
  handle), the token-bucket math, and `enforcement_decision` (the seam).
- `src/aime/auth.py` — the `tier` column, `UserRecord.tier`, `set_tier` /
  `set_tier_by_username`.
- `src/aime/config.py` — tier caps, bank days, thresholds, `tier_daily_cap`.
- `src/provider_backend.py` — debits in `_record_usage`; emits `usage_notice`.
- `src/aime/web_search_agent.py` — debits the offloaded search's cost (chat and
  agent runs both inject `quota_debit`).
- `src/aime/agents/runner.py` — takes the owning user's `QuotaMeter` (`quota=`)
  and threads it into the run's backend + web-search agent, so agent spend
  debits the same bucket. Never blocks here — see "Background agents" above.
- `src/aime/controller.py` — passes `usage_notice` through to frontends.
- `src/frontends/web_app.py` — `_usage_limits_armed()`, builds the per-user
  `QuotaMeter`, signup tier stamp, `/me` usage snapshot, the `/send` hard block
  (402 when over). `_user_over_budget()` is the on-demand agent-run gate (the
  `/agents/run`, `/agents/<id>/run`, `/schedules/<id>/run` routes); the
  autonomous `_scheduler_run_agent` skips it (recurring runs aren't blocked).
- `resources/style/web_chat.html` — the account meter, the `usage_notice`
  banner, and the over-budget composer lock (`__usageOver`, the 402 handler).
- `src/frontends/usage_dashboard.py` — the Accounts tier/usage columns and the
  Billing tab.
