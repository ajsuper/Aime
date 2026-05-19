# Production checklist

Things to do before exposing Aime to the open internet. The codebase is
hardened for local / personal use out of the box; this list covers the gap
between "runs on 127.0.0.1" and "safe to put behind a public DNS name".

## 1. Run behind TLS and set `AIME_HTTPS=1`

The session cookie's `Secure` flag is conditional on this env var. Without
TLS the cookie travels in cleartext on every request — fine on loopback,
unsafe on a public host.

```bash
export AIME_HTTPS=1
./scripts/web_app_serve.sh
```

Terminate TLS upstream (nginx, Caddy, ALB, Cloudflare) — Flask's built-in
server is not a production HTTP server and should never face the internet
directly.

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

## 8. Conscious tradeoffs to revisit

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
