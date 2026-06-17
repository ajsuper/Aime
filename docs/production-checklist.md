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

There is no "forgot password" flow. If you go public you'll want one —
build it before launch, not after a user gets locked out. The pluggable
`AuthBackend` interface is the right seam to add reset tokens to.

## 8. Usage limits and cost control

In `keys` / `billing` access mode, per-user **usage limits** are armed: a banked
daily cost allowance (token bucket) per tier, metered against real Anthropic
cost. See [usage-limits.md](usage-limits.md). Before going public:

- Pick tier caps (`AIME_TIER_LIGHT` / `AIME_TIER_POWER`) and the bank ceiling
  (`AIME_USAGE_BANK_DAYS`) for your budget — the defaults are tuned to the
  current cohort's averages.
- The **enforcement action** at an empty balance is now a hard block: `/send`
  refuses the turn (HTTP 402) with a calm "you've used up today's Aime — your
  access will be back tomorrow" message and the composer locks until the budget
  refills. (Previously notify-only.) The classification seam is
  `aime.quota.enforcement_decision`; see [usage-limits.md](usage-limits.md).
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
```
