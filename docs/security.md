# Security overview

A map of Aime's authentication and at-rest encryption: what is protected,
what is not, and the design choices behind each. Read this alongside
[`production-checklist.md`](./production-checklist.md), which covers the
operational steps for exposing the app beyond loopback.

## Threat model

Aime is designed for a small number of trusted users running on a single
host, optionally exposed behind TLS to the open internet. The properties it
tries to provide:

- **Account isolation.** A logged-in user can only read or modify their own
  conversations, topics, and calendar.
- **At-rest secrecy against partial leaks.** A stolen disk image, leaked
  backup tape, or sysadmin reading the user database in isolation cannot
  decrypt conversation files without also obtaining the host's
  `machine_secret` file.

What it deliberately does **not** try to protect against:

- **A full host compromise.** Aime's at-rest encryption is bound to a
  per-host `machine_secret` that lives on the same disk as the data it
  protects. An attacker who reads both the user database *and*
  `machine_secret` can decrypt every user's conversations. This is the
  correct trade-off for a personal-scale deploy where unattended
  background services (the midnight agent that sends morning briefs,
  pre-event reminders, etc.) need to read user data without the user
  being present. See [`midnight-agent.md`](./midnight-agent.md) for the
  feature it enables.
- **An attacker with code execution on the live server.** Per-user data
  keys are derived on demand from `machine_secret` and held in process
  memory while in use; a memory-reading attacker can extract them.
  Defense in depth here would require an HSM, an OS keychain, or a cloud
  KMS — see "Future hardening" below.
- **The model provider seeing the conversation.** Every turn is sent in
  cleartext to whichever model API is configured. This is intrinsic to
  using a hosted model; encryption at rest does not change it.
- **A privileged operator of the host.** Anyone with root on the server
  can read everything the server can read. If your threat model requires
  defense against the host operator, Aime should be self-hosted on a
  machine you control rather than run as a service for you by someone
  else.

---

## Authentication

### Password storage

Passwords are hashed with **Argon2id** (`argon2-cffi`), using the library's
tuned defaults (~64 MiB memory cost, 3 iterations, 4 lanes). Argon2id is
the current OWASP-recommended algorithm — memory-hard, side-channel
resistant, and what `PasswordHasher.check_needs_rehash` will keep current
as the defaults tighten over future library versions. See
[`auth.py`](../src/aime/auth.py).

### Lockout

After 5 failed login attempts in a 15-minute sliding window, an account is
locked for 15 minutes. State lives in `auth_attempts` rows in `auth.sql`,
so the lockout survives a server restart. The lockout is checked **before**
the password verify runs, so a locked account never spends CPU on a hash
verify and the error message is always identical regardless of password
correctness.

### Timing-safe handling of unknown usernames

When a username does not exist, the auth backend still runs an Argon2
verify against a pre-computed dummy hash. The wrong-password path and the
no-such-user path take the same wall-clock time, so an attacker cannot
enumerate accounts by timing differences. The error message — `"invalid
username or password"` — is identical for both cases.

### Username rules

Usernames are case-insensitive (sqlite `COLLATE NOCASE` on the column), so
`Alice` and `alice` collide. Allowed characters are `[A-Za-z0-9_.\-]`,
length 3–32. This rules out path-traversal-style characters that could be
relevant if usernames ever leaked into filesystem paths.

### Signup throttling

`/signup` is rate-limited per source IP (5 attempts per hour, sliding
window). Lockout-per-username doesn't help against signup, because an
attacker bulk-registering doesn't care which account name gets throttled.
The limiter is in-memory; restarts reset it, which is acceptable because
the threshold is small.

### Session secret key

Flask's session signing key is persisted to `secret_key` (mode 0600) so
session cookies survive restarts. Rotating the file invalidates every
active session. See `load_or_create_secret_key()` in `auth.py`.

### Sessions and cookies

The session cookie only ever contains `user_id` (an integer). The data key
used to decrypt user files is not stored in the cookie or in any in-memory
cache; it is re-derived on demand from the host's `machine_secret`. A
server restart does **not** force re-login — that is the property that
lets background services run after a restart without operator
intervention.

`HttpOnly` and `SameSite=Strict` are set on the cookie by default. `Secure`
(and HSTS) are sent whenever the browser-facing connection is HTTPS — set
`AIME_SECURE_COOKIES=1` when TLS terminates at a proxy in front
(`AIME_HTTPS=0`), or use `AIME_HTTPS=1` when the app terminates TLS itself
(which implies secure cookies). They are off only for plain-HTTP dev.

### CSRF

There is no CSRF token. Cross-site request forgery is mitigated by
`SameSite=Strict` on the session cookie — a request originating from
another site never carries the cookie, so a forged state-changing POST
arrives unauthenticated. The state-changing API also speaks JSON via
`fetch`, which adds a same-origin preflight for any non-simple request.
This is sufficient for the current threat model, but it is a **single
layer**: if you ever loosen `SameSite` (e.g. to `Lax` for a cross-site
embed or OAuth return), add explicit CSRF tokens at the same time.

---

## At-rest encryption

### Two-tier key scheme

```
machine_secret ──HKDF-SHA256(salt_dek)──▶ KEK ──AES-GCM──▶ DEK
DEK ──AES-GCM──▶ conversation files
```

- **DEK** (Data Encryption Key) — a random 256-bit key, minted once per
  user at signup. It's what actually encrypts conversation blobs and never
  changes for the lifetime of the account.
- **KEK** (Key Encryption Key) — derived on demand from the host's
  `machine_secret` and a per-user salt via HKDF-SHA256. Used only to
  encrypt/decrypt the DEK.
- **Wrapped DEK** — AES-GCM(KEK, DEK), stored alongside `salt_dek` in the
  `users` table.
- **`machine_secret`** — a 32-byte random file (`machine_secret`, mode
  0600) generated on first boot. The root of trust for at-rest
  encryption: anything that can read this file plus the user database can
  decrypt every user's data.

Why two tiers, instead of using `machine_secret` as the file key directly:

1. **Rotating `machine_secret` is O(users), not O(files).** Re-derive each
   user's KEK, re-wrap the same DEK, update one row per user. Existing
   conversation files don't need rewriting.
2. **The encryption key is decoupled from the password.** The Argon2 hash
   used to verify a login is purely authentication; it plays no part in
   encryption. This is what lets background services (the midnight
   agent) read user data without the user being present to type a
   password.
3. **Multiple wrappings of the same DEK become possible.** Adding a
   portable recovery passphrase later means adding a column, not
   re-encrypting files.

HKDF — not Argon2 — for the KEK derivation: `machine_secret` is 32 bytes
of OS randomness, not a low-entropy password. Argon2's slow-by-design
stretching buys nothing here and would cost ~150ms per unwrap.

See [`encryption.py`](../src/aime/encryption.py).

### What gets encrypted

| Data | Encrypted | Notes |
|---|---|---|
| `users/<id>/conversations/*.json.enc` | ✅ | AES-GCM with the session id as AAD. Filename binding prevents file-swap attacks. |
| `users/<id>/database.sql` | ❌ | Owned by the native backend (`serve.cpp`), which does not currently receive the data key. See **Known gaps** below. |
| `users/<id>/topics/*.md` | ❌ | Same as above. |
| `auth.sql` | ❌ | Contains password hashes + wrapped DEKs — the wrapped DEK is itself encrypted; the hashes are Argon2id. Treated as integrity-sensitive, not confidentiality-sensitive. |
| `machine_secret` | ❌ | Root of trust. Mode 0600. Must not be included in backups or copied off the host. |
| `secret_key` | ❌ | Flask signing key. Treat like a private key — file mode 0600. |

### AAD binding

Conversation files use the session id as AEAD additional-authenticated-data.
An attacker who renames Alice's encrypted file to look like Bob's session
will get an `InvalidTag` on decrypt, not a silent successful load. The
ciphertext is bound to its logical identity.

### Login flow

1. User submits username + password.
2. `verify()` checks lockout, performs the Argon2 verify on
   `password_hash`.
3. On success, derives the KEK from `machine_secret + salt_dek` and
   unwraps `wrapped_dek_v2` to recover the DEK.
4. Returns `(UserRecord, DEK, was_reinitialized)`. The Flask route stores
   `user_id` in the session cookie; the DEK is not cached — subsequent
   requests re-derive it on demand via `auth_backend.get_dek(user_id)`.
5. All subsequent requests pass through `login_required`, which checks
   the cookie's `user_id` resolves to a live user.

`was_reinitialized` flags an early-beta account that was on the v1
password-derived scheme and has just been auto-upgraded to v2 — see
[Legacy encryption migration](#legacy-encryption-migration) below.

### Logout flow

`/logout` clears the session cookie. There is no in-memory key to wipe.

### Background unwrap

`auth_backend.get_dek(user_id)` returns the user's DEK without a password.
This is what the midnight agent calls to read a user's events/topics at
07:00 to write a morning brief. The implication is documented in the
threat model: a host compromise can decrypt any user's data; this is the
explicit trade-off for the feature.

### Legacy encryption migration

Aime is in early beta. The v1 scheme used a password-derived KEK
(`Argon2id(password, salt_kek)` wrapping the DEK); the v2 scheme replaces
it with the machine-secret-derived KEK described above. Existing v1
accounts are auto-upgraded on next login:

1. `verify()` authenticates the password as usual.
2. The row's `enc_version` is detected as `< 2`.
3. A fresh v2 DEK is generated, wrapped under `machine_secret + new
   salt_dek`, and persisted; the old `salt_kek` / `wrapped_dek` columns
   are nulled.
4. The Flask layer calls `controller.delete_all_sessions()` to wipe the
   user's `*.json.enc` files — they were encrypted under the old DEK,
   which is gone, so they're unreadable garbage either way.

The user's database (topics, calendar, preferences) and account state
(api_access, lockout history) are untouched. Conversation history is
treated as a nice-to-have for cross-device sync, not as data worth
preserving across a one-time scheme change.

Search the codebase for `LEGACY MIGRATION AUTH` to find every transitional
code path; they are paired with `END LEGACY MIGRATION AUTH` markers and
can be deleted once you have decided all installs have upgraded.

---

## Email verification and account recovery

Gated entirely behind **`DO_EMAIL_VERIFICATION`** (default `0`). When off,
none of the routes below are reachable — they redirect to `/login`, and
the system behaves as password-only with no self-service reset. When on,
it requires working SMTP (see `email_send.py` / `SMTP_*` env in
[`production-checklist.md`](./production-checklist.md)). All flows share
one `email_verifications` table and a single short-lived emailed code.

| Flow | Routes | Backend |
|---|---|---|
| **Signup verification** | `/signup/verify*` | `start_/complete_signup_verification` — a new account is unusable until its email code is entered. |
| **Login second factor** | `/login/verify*` | `start_/complete_login_verification` — after a correct password, a one-time code is mailed and the session is withheld until it's entered. |
| **Add email** | `/add-email*` | `start_/complete_add_email_verification` — stamps an email onto an account that predates verification. |
| **Forgot password** | `/forgot`, `/forgot/verify*` | `start_/complete_password_reset` — emailed code authorizes a new password. |
| **Account recovery** | `/account/recover` | `restore` — un-deletes a soft-deleted account on next login; re-stamps `api_access` so a recovered account can't skip the access gate. |

Security properties that matter:

- **Enumeration-safe.** `/signup` and `/forgot` advance to the same next
  page and send the same response whether or not an account matched, so a
  prober cannot tell a real address from a miss. (Note this is the
  *opposite* posture from known-gap #6 below, which is about `/signup`'s
  "username already taken" message — the email flows are the hardened
  ones.)
- **Per-IP throttled.** Reset requests are rate-limited per source IP so
  the form can't be used to bomb a victim's inbox; over the limit it
  silently skips sending but still advances, preserving enumeration safety.
- **Fails closed.** If mail is down during a required login second factor,
  the login is **refused** (HTTP 502) rather than bypassing the factor.
- **Trusted device.** A "remember this device" cookie token
  (`is_trusted_device`) lets a known browser skip the emailed code; it is
  the only thing that shortcuts the second factor.

---

## Known gaps and compromises

The following are conscious tradeoffs in the current implementation. They
are acceptable for a small, trusted-host deployment; some should be
addressed before exposing Aime to an untrusted user base.

### 1. The native backend's data is not encrypted

`users/<id>/database.sql` and `users/<id>/topics/*.md` are written by the
native backend (`serve.cpp`), which has no access to the per-user data
key. Until the backend is folded into the Python process — or extended to
receive the data key and use SQLCipher plus an authenticated-encryption
layer for topic files — calendar entries and topic content remain
plaintext at rest.

Conversation history is encrypted; calendar and topic data are not. If
your threat model requires the whole user folder to be encrypted, this is
the open item to track.

### 2. The model provider sees plaintext conversations

Every turn is sent in cleartext to the model provider's API (Anthropic by
default). This is intrinsic to using a hosted model and is independent of
how data is stored locally. Encryption at rest does not change it. For
fully private operation, the application would need to be reconfigured
against a self-hosted model.

### 3. The server can decrypt any user's data

This is by design — the v2 encryption scheme binds the DEK to a host-side
`machine_secret`, not to the user's password, so the assistant can read
user data while the user is offline. Without this property, the midnight
agent (morning briefs, pre-event reminders) could not function.

Operational consequences:

- A full host compromise (disk + `machine_secret`) decrypts every user's
  conversations.
- A forgotten password no longer destroys data — password reset is
  trivial to add and does not require touching encryption.
- Backups that include the user database **must not** include
  `machine_secret`. Restoring to a new host requires bringing the secret
  across, or accepting that the encrypted conversations are lost (the
  database — topics, calendar, preferences — survives either way).

### 4. The data key is reachable on the live host

Anyone with code execution on the running server can read
`machine_secret`, look up any user's wrapped DEK, and unwrap it. There is
no in-memory key cache to evict; the DEK is re-derived on demand. The
hardening path is to move `machine_secret` off the local disk — see
"Future hardening" below.

### 5. Second factor is email-only and opt-in

When `DO_EMAIL_VERIFICATION=1` (see [Email verification and account
recovery](#email-verification-and-account-recovery)), login carries an
**emailed one-time code** as a second factor, with an optional
"remember this device" trusted-device token that skips the code on a
known browser. There is no app-based second factor (TOTP, WebAuthn);
email possession is the only supported factor. When
`DO_EMAIL_VERIFICATION` is off (the default), login is single-factor
(password only). The `AuthBackend` protocol in `auth.py` is the seam for
adding a stronger factor without disturbing the rest of the system.

### 6. `/signup` discloses whether a username exists

The error message distinguishes "username already taken" from other signup
failures, which is useful UX but allows enumeration of which accounts
exist. A public-facing deployment should switch to a generic "signup
failed" response.

### 7. There is no built-in backup of encrypted data

Encryption protects confidentiality, not availability. A lost or corrupted
disk results in lost data even when encryption is working as designed. A
user-controlled backup feature (local filesystem or download) is a planned
enhancement; until it exists, operators are responsible for snapshotting
the data directory.

### 8. Session storage uses signed cookies

The session cookie holds only `user_id`, signed with `secret_key`. It
never holds the DEK. Rotating `secret_key` is the operator's mechanism for
invalidating every active session at once; it is not exposed as a UI
control.

---

## Verification

A quick smoke test that confirms the encryption layer is wired correctly:

```bash
# Sign up a new user via the web UI, then:
ls $HOME/.local/share/aime-assistant/database/users/<id>/conversations/
# Expect: *.json.enc files, no plaintext *.json

xxd -l 64 $HOME/.local/share/aime-assistant/database/users/<id>/conversations/*.json.enc | head
# Expect: high-entropy bytes; no recognizable JSON keys like "role" or "content"

sqlite3 $HOME/.local/share/aime-assistant/database/auth.sql \
    'SELECT id, username, enc_version, length(salt_dek), length(wrapped_dek_v2) FROM users'
# Expect: enc_version=2, salt_dek=16, wrapped_dek_v2=60 for every active user

ls -l $HOME/.local/share/aime-assistant/database/machine_secret
# Expect: mode 0600, 32 bytes
```

---

## Future hardening

The current design is fit for a personal-scale deploy. Two paths exist to
narrow the "host compromise = full decrypt" surface, both deferred:

1. **OS keychain** for `machine_secret`. On Linux (libsecret), macOS
   (Keychain), and Windows (DPAPI) the root secret can live outside the
   filesystem so a disk-only snapshot — even one that includes the user
   database — cannot decrypt anything.
2. **Cloud KMS** (AWS KMS, GCP KMS, HashiCorp Vault). The root unwrap
   becomes a network call; the secret never sits on the host at all.
   Appropriate for managed deploys where the host is fungible.

Either substitution is a one-function change in
[`encryption.load_or_create_machine_secret`](../src/aime/encryption.py) —
the wrap/unwrap logic that depends on the 32-byte result stays the same.
