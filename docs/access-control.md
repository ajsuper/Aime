# Access control

How Aime decides whether a user may send messages through the paid model
backend. This covers the `api_access` flag, the `AIME_ACCESS_MODE` deployment
modes, the invite-key system, and the configurations that make sense (and the
ones that misbehave).

## Two layers

Access control is deliberately split into two independent layers:

1. **`api_access` — the per-user flag.** A column on the `users` table in
   `auth.sql`. It is the *single source of truth* for "can this user send
   messages." It is plain persistent state: never recomputed, never reset by
   restarts or config changes. Billing, invite keys, and the admin tool all do
   one thing — flip this flag.

2. **`AIME_ACCESS_MODE` — how the flag gets granted.** A deployment-time
   setting. It is not permanent infrastructure; it is a mode switch that can
   be swapped (today `open`/`keys`, later `billing`) without touching layer 1.

This split is what keeps the invite-key system from being a permanent
commitment: it lives entirely behind `AIME_ACCESS_MODE=keys` and can be
replaced by a billing mode later, while `api_access` stays.

## The one rule

**`api_access` is the only thing consulted to allow/deny a message. The mode
only does two things:**

1. **At account creation**, it picks the value stamped into the new row:
   `open` → `1`, `keys` → `0`.
2. **At `/send`**, the gate is armed only in `keys` mode. In `open` mode
   `/send` skips the check entirely.

There is no startup database mutation and no recomputation. A restart or a
mode flip never changes a stored `api_access` value.

### Why this is not messy across restarts

| You do this                     | What happens                                                                 |
|----------------------------------|------------------------------------------------------------------------------|
| Restart, same mode               | Nothing changes; `api_access` is on disk in `auth.sql`.                       |
| User redeems a key               | Their `api_access` → `1`, permanent — survives every restart and mode flip.   |
| `open` → `keys`, restart         | Gate arms. Everyone created during `open` has `1`, so they are grandfathered automatically. Only unredeemed `keys`-era accounts are `0`. |
| `keys` → `open`, restart         | Gate disarms; everyone can send. Redemption records remain untouched.          |
| `keys` → `open` → `keys`         | The second `keys` stint sees the same stored values — every prior redemption still counts. |

Because `open`-mode signup stamps `1`, anyone who ever had an account during
an `open` period is permanently grandfathered. The *only* row that can hold
`0` is a `keys`-era account that has not redeemed a key.

## The modes

- **`keys`** (default) — new accounts are stamped `api_access=0`; `/send` is
  gated. Users gain access by redeeming a single-use invite key (or via an
  admin grant). This is the safe configuration for exposing Aime to other
  people, and it is the default so a misconfigured deployment fails closed.
- **`open`** — `/send` is not gated; new accounts are stamped `api_access=1`.
  This is local / personal / fully-trusted use, and must be chosen explicitly.
- **`billing`** — behaves like `keys` for the send gate, but **Stripe owns
  `api_access`** (the invite-key path goes dormant). Like `keys` it **arms usage
  limits** (see [usage-limits.md](usage-limits.md)), where a user's *tier* is
  their subscription plan: a signed Stripe webhook sets `api_access` + `tier`
  together from the live subscription, with a card-at-signup 30-day trial. Fully
  documented in [billing.md](billing.md). The app refuses to start in this mode
  unless Stripe is configured (otherwise no one could gain access).

## Usage limits

Independently of the send gate, `keys` and `billing` modes **arm per-user usage
limits**: a banked daily cost allowance (token bucket) per tier, metered against
real Anthropic cost. `open` mode disarms them (trusted local use). This is the
budget that protects your spend; it is fully documented in
[usage-limits.md](usage-limits.md). The two layers are orthogonal — `api_access`
is *whether* a user may send; the usage budget is *how much* they may spend.

## Invite keys

Used only in `keys` mode. Properties:

- **Single-use.** Redeeming a key consumes it; it cannot be reshared.
- **Hashed at rest.** Only the SHA-256 hash of a key is stored. The raw key is
  shown once at generation and is then unrecoverable.
- **High entropy** (~192 bits), so redemption is not brute-forceable.
- **Revocable before redemption**, and auditable after (which key → which
  user) — the natural runway to a billing system.

Managed with `scripts/access_keys.py`:

| Command                            | Purpose                                              |
|------------------------------------|------------------------------------------------------|
| `gen [N] [--note TEXT]`            | Mint N single-use keys (printed once).               |
| `list`                             | List users (with their flag) and all keys.           |
| `grant <username>`                 | Directly set `api_access=1` — admin override.        |
| `revoke <username>`                | Directly set `api_access=0` — the over-limit switch. |
| `tier <username> <light\|power>`   | Set the user's usage-limit tier (daily cost allowance). |
| `revoke-key <key>`                 | Kill an unredeemed key.                              |
| `revoke-all`                       | Zero `api_access` for everyone — billing cutover.    |

## Account lifecycle

Accounts are deleted in two stages, so a mistake (or a user who changes their
mind) is recoverable.

1. **Soft delete.** Setting `deleted_at` on the `users` row flags the account.
   It immediately stops working — `verify()` no longer returns a session,
   `lookup()` and `list_users()` skip it, so it appears not to exist — but the
   user's data directory is left **completely untouched**. A user soft-deletes
   their own account from the web profile menu (`POST /account/delete`); an
   admin can do it too.

2. **Permanent purge.** After a grace period (default **30 days**) a
   soft-deleted account becomes eligible for purge: a final backup zip is
   written, the row is hard-deleted, and the data directory is removed. Purge
   is an explicit admin action — it never happens automatically.

### Recovery

While an account is soft-deleted, recovery is just signing in again. A login
with the correct password raises `AccountDeleted` instead of returning a
session; the web frontend turns that into a prompt — *"This account no longer
exists. Do you want to recover it?"* — and `POST /account/recover` clears the
flag.

Recovery re-stamps `api_access` using the same rule as signup: `open` mode
grants it, `keys` mode withholds it (the user must redeem a key again). This
prevents a soft-deleted account from sidestepping the access gate on its way
back. Admin restores via `scripts/manage_users.py restore` default to
`api_access=0` regardless of mode — the admin can grant explicitly via
`access_keys.py grant` afterwards.

**Verification is the password, and it is cryptographically self-enforcing.**
The check is only ever raised *after* a correct password verify, so a deleted
account is indistinguishable from a live one to anyone who does not already
know the password. And because conversations are encrypted under a key derived
from that password, even a mistakenly-restored account is unreadable to anyone
without it. There is deliberately **no recovery path after purge** — purge is
final, which is why the grace period exists.

The username stays reserved (the `UNIQUE` constraint) while soft-deleted, so a
new signup cannot take a deleted account's name until it is purged.

Managed with `scripts/manage_users.py`:

| Command                            | Purpose                                              |
|------------------------------------|------------------------------------------------------|
| `list`                             | List active accounts and ones pending purge.         |
| `delete <username>`                | Soft-delete an account (reversible).                 |
| `restore <username>`               | Clear the soft-delete flag.                          |
| `purge [--days N] [--dry-run]`     | Permanently remove accounts past the grace period.   |

## Configurations

Behavior comes from two independent knobs: `AIME_ALLOW_SIGNUP` (`0`/`1`) and
`AIME_ACCESS_MODE` (`open`/`keys`).

| Signup | Mode   | Behavior                                                        | When it makes sense |
|--------|--------|-----------------------------------------------------------------|---------------------|
| `1`    | `open` | Anyone registers **and** can send immediately                   | Local / loopback only. Never internet-exposed (see below). |
| `1`    | `keys` | Anyone registers; no messages until they redeem a key           | The safe public-facing config; the self-registration / onboarding phase. |
| `0`    | `open` | No new accounts; everyone who exists can send                   | Steady state for a closed, fully-trusted group.            |
| `0`    | `keys` | No new accounts; access is per-user via keys / grants           | Locked-down steady state; pre-billing holding state.       |

### Example: free test users, then billing

1. **Testing** — `signup=1, mode=keys`. Testers self-register; hand each a
   single-use key. Free access, fully controlled.
2. **Lock the cohort** — flip `signup=0` (still `keys`). No new freeloaders;
   existing testers keep working.
3. **Billing** — set `mode=billing` and configure Stripe (see
   [billing.md](billing.md)). Cutover caveat: testers currently hold
   `api_access=1`, so they'd be grandfathered into free access with no
   subscription. Run `access_keys.py revoke-all` once at cutover and let the
   billing webhook re-grant on payment.

### Example: open-source friends self-host

1. **Onboarding** — `signup=1, mode=keys`. Friends self-register; hand out
   keys.
2. **Lock it** — flip `signup=0`, stay on `mode=keys` permanently. That is the
   stable end state.

## Weird / dangerous combinations

- **🔴 `signup=1` + `mode=open` + internet-exposed.** Any stranger registers
  and spends your Anthropic budget. `mode` defaults to `keys` so this never
  happens by accident — reaching it requires explicitly choosing `open`. The
  `web_app_serve.sh` prompt additionally warns and asks for confirmation when
  `bind` is off-host + `signup=1` + `mode=open` line up.
- **🟡 Bootstrap lockout.** There is no admin "create account" command — the
  signup route is the *only* way accounts are created, and in `keys` mode even
  the first account defaults to `api_access=0`. Bootstrap is always:
  1. Start with `signup=1`, register your own account.
  2. `scripts/access_keys.py grant <you>`.
  3. Then flip `signup=0`.
- **🟡 Flipping to `open` is not temporary.** Anyone who registers during an
  `open` window is stamped `api_access=1` permanently; switching back to
  `keys` does not revoke them. For a genuinely temporary "everyone in" window,
  run `revoke-all` afterward.
- **🟢 Not weird, just useful.** Redeeming a key works in `open` mode too and
  still sets `api_access=1` — so you can pre-grant people before switching to
  `keys`. Toggling `AIME_ALLOW_SIGNUP` never affects existing users; it only
  gates the registration route.

The two `keys` rows are the safe public configurations. The two `open` rows
are trusted-environment only. Nothing behaves randomly — the only sharp edges
are the bill-bomb combo and the bootstrap lockout, both deployment-time
mistakes guarded by prompts and this document.

## Admin dashboard

The admin web dashboard (`src/frontends/usage_dashboard.py`) is a web version
of the `scripts/` admin tools, so a container deployment can be managed
without shell access. It is enabled with `AIME_USAGE_DASHBOARD=1` and served on
port 5050. Four tabs:

- **Overview** / **Cache Efficacy** — usage statistics from `usage.jsonl`.
- **Accounts** — list / grant / revoke send access, set each user's usage **tier**
  (with a live **Usage** column), soft-delete, restore, and purge expired accounts.
- **Keys** — mint and revoke invite keys.
- **Billing** — the tier allowances and, in billing mode, each subscriber's
  Stripe status (read-only). See [billing.md](billing.md).

It wraps the **same** `auth.py` / `accounts.py` functions the CLIs use — no
access logic is duplicated in the frontend.

Because it can disable accounts and spend money, the whole dashboard is
**password-gated**: `AIME_ADMIN_PASSWORD` must be set or the app refuses to
start. A signed session cookie keeps the admin logged in; `SameSite=Lax` plus
a per-session CSRF token guard the state-changing POSTs. It still binds
loopback unless `AIME_USAGE_DASHBOARD_HOST` is set explicitly.

## Implementation map

- `src/aime/auth.py` — the `api_access` column, the `deleted_at` column, the
  `access_keys` table, and the access-control / account-lifecycle API
  (`set_api_access`, `redeem_key`, `generate_access_key`, `list_access_keys`,
  `revoke_access_key`, `revoke_access_key_by_hash`, `list_users`,
  `soft_delete`, `restore`, `hard_delete`, `list_deleted_users`). The single
  source of truth.
- `src/aime/accounts.py` — purge orchestration: final backup, hard-delete,
  data-directory removal, and the grace-period bookkeeping.
- `scripts/access_keys.py` — the access-control / invite-key admin CLI.
- `scripts/manage_users.py` — the account-lifecycle admin CLI (delete /
  restore / purge).
- `src/frontends/usage_dashboard.py` — the password-gated web admin dashboard;
  a thin wrapper over the same `auth.py` / `accounts.py` surface.
- `src/frontends/web_app.py` — reads `AIME_ACCESS_MODE`; stamps the signup
  default; gates `/send` with the `api_access_required` decorator; serves
  `/redeem` (key redemption), `/account/delete` and `/account/recover`; and
  exposes `access_mode` + `api_access` on `/me`. Login never depends on
  `api_access` — a user without send access still logs in and can read all
  their data. In `billing` mode it also serves the `/billing/*` routes and
  refuses to start unless Stripe is configured.
- `src/aime/billing.py` — the Stripe layer for `billing` mode: the webhook
  reconcile that sets `api_access` + `tier` from a subscription (see
  [billing.md](billing.md)). Dormant in `keys`/`open`.
- `resources/style/web_chat.html` — the invite-key field and the delete-account
  button in the Account modal, the Billing tab (billing mode), and composer
  locking when sending is gated.

All of these stay thin and consistent because the access-control and
account-lifecycle logic lives on `auth.py` / `accounts.py`, not in any
frontend.
