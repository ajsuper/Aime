# Production checklist

Things to do before exposing Aime to the open internet. The codebase is
hardened for local / personal use out of the box; this list covers the gap
between "runs on 127.0.0.1" and "safe to put behind a public DNS name".

## 1. Run behind a TLS-terminating proxy (recommended)

The hardened topology is: a reverse proxy (nginx, Caddy, ALB, Cloudflare)
terminates TLS for your domain and forwards plain HTTP to the app on
loopback. In this mode the app runs on **waitress** (a production WSGI
server) — the built-in development server is never internet-facing.

```bash
export AIME_HTTPS=0           # app serves HTTP; the proxy does TLS
export AIME_SECURE_COOKIES=1  # browser-facing connection is HTTPS
export AIME_TRUSTED_PROXY_HOPS=1   # see §3
./scripts/web_app_serve.sh
```

`AIME_SECURE_COOKIES=1` is what keeps the session cookie's `Secure` flag and
HSTS on when TLS terminates upstream (they no longer key off `AIME_HTTPS`).
Without it the cookie can leak over a stray plain-HTTP request. The Docker
`.env.example` already sets these defaults.

If instead the app must terminate TLS itself (single box, no proxy — e.g. for
LAN microphone access), set `AIME_HTTPS=1`; that path uses the built-in
development server and is **not** recommended for internet-facing hosts.

## 2. Keep the C++ backend off the network

`serve.o` binds `127.0.0.1:8080` by default. **Leave it that way.** It has
no auth of its own — security comes from the fact that only the local
Flask process can reach it. Never expose port 8080 publicly, in a firewall
rule, or via a reverse proxy.

## 3. Reverse-proxy configuration

If you put Flask behind a reverse proxy, two things need attention:

- **`ProxyFix`**: install Werkzeug's `ProxyFix` middleware so
  `request.remote_addr` reflects the real client IP. Without it, the
  signup rate limiter sees every request as coming from the proxy and
  caps all traffic together.
- **Trusted-header policy**: decide which forwarded headers to trust
  (`X-Forwarded-For`, `X-Forwarded-Proto`) before turning them on. An
  untrusted forwarded header is worse than none — it lets an attacker
  spoof their source IP and bypass the rate limit.

## 4. Periodic argon2-cffi upgrades

Password hashes are stored with the library's tuned defaults. As those
defaults tighten, `check_needs_rehash` will return True on next login and
each user's hash will be upgraded automatically. Make sure
`requirements.txt` allows that movement: bump `argon2-cffi` versions when
you bump other deps so the upgrades actually happen.

### Dependency security cadence

`requirements.txt` is **fully pinned** (`==`) for reproducible builds, which
means *no* security patch reaches you without a deliberate bump. Establish a
recurring review (e.g. `pip list --outdated`, Dependabot, or `pip-audit`) and
prioritise the libraries that parse untrusted input:

- **`pillow` / `pillow-heif`** — decode user-uploaded images; a recurring CVE
  surface. Treat their advisories as high priority.
- **`cryptography`** — the at-rest encryption and TLS primitives.
- **`flask` / `requests` / `waitress`** — the request path and all outbound
  HTTP (messaging, web search).
- **`argon2-cffi`** — also drives the rehash-on-login upgrade above.

## 5. Backup strategy

Everything that matters lives under `$AIME_DATABASE_DIR`
(default `~/.local/share/aime-assistant/database`):

- `auth.sql` — accounts (cannot be regenerated)
- `secret_key` — session signing key (rotating it logs everyone out)
- `users/<id>/database.sql` — per-user calendar + topic metadata
- `users/<id>/topics/*.md` — topic content

Snapshot the whole directory. Excluding `secret_key` and restoring an
older snapshot is fine — users will just have to log in again.

## 6. Logging and monitoring

Currently the auth backend logs nothing on failure. For a public
deployment, consider tailing Flask access logs into a tool that alerts on
abnormal `/login` 401/429 rates or `/signup` 429s.

## 7. Account recovery

A self-service **forgot-password** flow exists (`/forgot` → emailed code →
new password) along with **email login verification** and **soft-delete
account recovery** — but all of it is gated behind `DO_EMAIL_VERIFICATION=1`
and needs working SMTP (see §10). With the flag **off** (the default) there
is *no* reset path, so a locked-out user is stuck. Before going public:

- Set `DO_EMAIL_VERIFICATION=1` and configure SMTP so the reset flow is
  actually reachable.
- Verify the flow end-to-end (request a code, reset, log in) on the real
  mail transport — a misconfigured `SMTP_*` silently disables every email
  flow, including the login second factor, which then **fails closed**
  (logins refused with HTTP 502) rather than letting users in.

See [security.md → Email verification and account recovery](security.md#email-verification-and-account-recovery)
for the full surface.

## 8. Usage limits and cost control

In `keys` / `billing` access mode, per-user **usage limits** are armed: a banked
daily cost allowance (token bucket) per tier, metered against real Anthropic
cost. See [usage-limits.md](usage-limits.md). Before going public:

- Pick tier caps (`AIME_TIER_LIGHT` / `AIME_TIER_POWER`) and the bank ceiling
  (`AIME_USAGE_BANK_DAYS`) for your budget — the defaults are tuned to the
  current cohort's averages.
- The **enforcement action** at an empty balance is now a hard block: `/send`
  refuses the turn (HTTP 402) with a calm "you've used up today's Aime — your
  access will be back tomorrow" message and the composer locks until the next
  daily top-up. (Previously notify-only.) The classification seam is
  `aime.quota.enforcement_decision`; see [usage-limits.md](usage-limits.md).
  Note the block is **one turn behind**: the check is pre-turn but the debit is
  in-turn, so a user can overshoot their remaining balance by a single expensive
  turn before the next send is refused. The budget bounds steady-state spend,
  not one turn's cost — fine for the free-tester cohort, but size tier caps with
  that headroom in mind. The debit fails open and logs at `warning` on failure;
  watch that log line — it means cost control is silently off.
- `open` mode disarms limits entirely — never use it internet-facing.

## 9. Conscious tradeoffs to revisit

These are acceptable for personal scale; reconsider before going public:

- **`/signup` reveals "username already taken"** — useful UX, but lets
  an attacker enumerate existing accounts. Swap for a generic "signup
  failed" message at higher scale.
- **No global per-endpoint rate limiting** beyond `/login` (per-account)
  and `/signup` (per-IP). A logged-in user can DoS their own session.
  Acceptable for known users; harden if accounts are free to create.
- **TLS is the operator's job.** Flask never serves TLS itself.
- **Topic markdown sanitization is client-side** (DOMPurify). A JS-disabled
  client would see raw markdown — fine, the app needs JS anyway.

## 10. Email (SMTP) configuration

Required if you turn on `DO_EMAIL_VERIFICATION=1` (§7): signup verification,
the login second factor, add-email, and password reset all send mail through
`email_send.py`. Configure the SMTP account in the environment:

| Variable | Default | Meaning |
|----------|---------|---------|
| `SMTP_HOST` | `smtp.gmail.com` | Submission server hostname. |
| `SMTP_PORT` | `587` | Submission port (STARTTLS). |
| `SMTP_USERNAME` | `EMAIL_ADDRESS` | Login username. |
| `SMTP_PASSWORD` | `EMAIL_PASSWORD` | Login password / app password. |
| `SMTP_FROM` | `SMTP_USERNAME` | From address. |

The legacy `EMAIL_ADDRESS` / `EMAIL_PASSWORD` pair still works on its own.
**Verify mail actually sends before launch:** a misconfigured account
silently disables every email flow, and because the login second factor
fails *closed*, that turns into users being refused at login (HTTP 502),
not a soft degradation.

## 11. Terms of Service and Privacy Policy — **BLOCKING**

Signup requires the user to tick a box agreeing to the Terms and Privacy
Policy. The plumbing is done; **the documents themselves are placeholders and
must be replaced before you open signups.**

- The text lives in `resources/legal/terms.html` and
  `resources/legal/privacy.html` (bodies only — `resources/style/legal.html`
  supplies the page shell). They are served at `/terms` and `/privacy`, public
  and unauthenticated, because the signup form links to them.
- Both currently open with a **"Draft — not yet in force"** banner and are
  peppered with `[BRACKETED]` blanks (entity name, jurisdiction, contact
  addresses, retention periods, sub-processors). They have **not** been
  reviewed by a lawyer and are not binding.
- The privacy draft asserts things about how the code behaves — encryption at
  rest, the 30-day deletion window, which third parties see your data. Check
  each against *your* deployment before publishing it.
- `AIME_TERMS_VERSION` (default: the date in `aime.config.TERMS_VERSION`)
  records *which* revision each account agreed to, stamped onto
  `users.terms_accepted_at` / `users.terms_version` at signup. **Bump it
  whenever the documents change materially** — otherwise old acceptances read
  as agreement to text those users never saw. Keep it in step with the
  "Version" line inside the documents.
- Accounts created before this feature have both columns NULL: we record only
  consent actually witnessed rather than backfilling one nobody gave. If you
  need existing users on the new terms, prompt them — the columns tell you who
  is outstanding.

```bash
# No draft banner and no unfilled blanks should survive to launch
curl -s https://your-domain/terms   | grep -c 'draft-banner\|class="blank"'   # expect 0
curl -s https://your-domain/privacy | grep -c 'draft-banner\|class="blank"'   # expect 0
```

## Quick verification before launch

```bash
# 1. CSP/headers are reaching the browser
curl -sI https://your-domain/login | grep -i 'content-security-policy\|x-frame'

# 2. Backend is not externally reachable
nmap -p 8080 your-domain   # should report closed/filtered

# 3. Session cookie carries the hardening flags
curl -sI https://your-domain/login | grep -i 'set-cookie'
# expect: HttpOnly; Secure; SameSite=Strict

# 4. Logout requires POST
curl -sX GET https://your-domain/logout    # expect 405
curl -sX POST https://your-domain/logout   # expect 302 → /login

# 5. Signup refuses consent-less posts (the checkbox is not the real gate)
curl -sX POST https://your-domain/signup \
  -d 'username=probe&password=Sufficiently-long-pw-1&password2=Sufficiently-long-pw-1'
# expect 400 and no account created

# 6. Fonts are served by us, not Google (the CSP would block a remote load,
#    and the page would fall back to system faces without telling anyone)
curl -s https://your-domain/login | grep -c 'fonts.googleapis.com'   # expect 0
curl -sI https://your-domain/fonts/fraunces-latin.woff2 | head -1    # expect 200
```

### A note on webfonts

Fraunces and Hanken Grotesk are **self-hosted** in `resources/style/fonts/`
and served from `/fonts/`. This is deliberate, not incidental: the CSP allows
neither `fonts.googleapis.com` (`style-src`) nor `fonts.gstatic.com`
(`font-src`), so a Google Fonts `<link>` fails *silently* — the page renders in
Georgia / system-ui and nothing logs an error. It also means no third-party
request, and no visitor IP handed to Google, on the pre-login pages. If you add
a page, link `/fonts/fonts.css`; `tests/test_fonts.py` will fail if you don't.
