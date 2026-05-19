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
- **At-rest secrecy of conversation history.** A stolen disk, leaked backup
  tape, or sysadmin with read access to the data directory cannot read what
  users said to the assistant.
- **Password-derived secrets.** The keys that decrypt user data are
  reproducible only from a correct password — they are not persisted on
  disk in any form a server compromise could recover.

What it deliberately does **not** try to protect against:

- **An attacker with code execution on the live server.** Per-user data
  keys live in process memory while a session is active; a memory-reading
  attacker can extract them. Defense in depth here would require an HSM or
  a dedicated key-management service, neither of which fit a personal-scale
  deploy.
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
used to decrypt user files is held in a separate, **in-process memory
cache** (`_dek_cache` in `web_app.py`). On a server restart, the cache
empties; users with valid cookies are forced to re-authenticate. This is
the correct property — the cookie alone never grants access to encrypted
data.

`HttpOnly` and `SameSite=Lax` are set on the cookie by default; `Secure` is
set when `AIME_HTTPS=1`.

---

## At-rest encryption

### Two-tier key scheme

```
password ──Argon2id(salt_kek)──▶ KEK ──AES-GCM──▶ DEK
DEK ──AES-GCM──▶ conversation files
```

- **DEK** (Data Encryption Key) — a random 256-bit key, minted once per
  user at signup. It's what actually encrypts conversation blobs and never
  changes for the lifetime of the account.
- **KEK** (Key Encryption Key) — derived from the password via Argon2id
  with its own salt, distinct from the password-verifier salt. Used only
  to encrypt/decrypt the DEK.
- **Wrapped DEK** — AES-GCM(KEK, DEK), stored alongside `salt_kek` and the
  password hash in the `users` table.

Why two tiers, instead of using the password as the file key directly:

1. **Password changes are O(1).** Re-derive the KEK from the new password,
   re-wrap the same DEK, update one row. Existing files don't need
   rewriting.
2. **The auth hash and the encryption key are decoupled.** The Argon2 hash
   used to verify a login (PHC string, salted separately) and the KEK used
   to unwrap the DEK use independent salts. Leaking one gives no advantage
   against the other.
3. **Multiple wrappings of the same DEK become possible.** Adding a
   recovery key, a backup passphrase, or a second-device key later means
   adding a row, not re-encrypting files.

See [`encryption.py`](../src/aime/encryption.py).

### What gets encrypted

| Data | Encrypted | Notes |
|---|---|---|
| `users/<id>/conversations/*.json.enc` | ✅ | AES-GCM with the session id as AAD. Filename binding prevents file-swap attacks. |
| `users/<id>/database.sql` | ❌ | Owned by the native backend (`serve.cpp`), which does not currently receive the data key. See **Known gaps** below. |
| `users/<id>/topics/*.md` | ❌ | Same as above. |
| `auth.sql` | ❌ | Contains password hashes + wrapped DEKs — the wrapped DEK is itself encrypted; the hashes are Argon2id. Treated as integrity-sensitive, not confidentiality-sensitive. |
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
3. On success, derives the KEK from the password + `salt_kek`, unwraps the
   `wrapped_dek` to recover the DEK.
4. Returns `(UserRecord, DEK)`. The Flask route stores `user_id` in the
   session cookie and the DEK in the in-process cache, keyed by `user_id`.
5. All subsequent requests pass through `login_required`, which checks the
   cookie's `user_id` is valid **and** that a DEK exists in the cache. If
   either is missing, the user is forced to re-login.

### Logout flow

`/logout` clears the session cookie and deletes the DEK from the in-process
cache.

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

### 3. A forgotten password means lost data

Without the password, the KEK cannot be re-derived, and without the KEK
the wrapped DEK cannot be unwrapped. There is no password-reset flow. This
is the honest consequence of "encrypted at rest" — a reset path operable
by the server administrator would also let that administrator read user
data without consent.

A user-facing recovery flow can be added later without re-encrypting any
files: at signup, generate a separate recovery key and wrap the same DEK
under it; display the recovery key to the user once; store only the extra
wrapped blob server-side. This is a planned enhancement, not a current
feature.

### 4. The data key lives in process memory during a session

An attacker who can execute code on the running server can read the
in-memory key cache and recover every logged-in user's data key. Avoiding
this entirely would require an external key-management service or an HSM,
which is out of scope for the intended deployments.

Mitigations in the current design:

- The DEK is never persisted outside of its wrapped-by-KEK form.
- A server restart drops the entire in-memory cache, forcing every user
  to re-authenticate (and re-derive their KEK) before any data can be
  decrypted.

### 5. Login is single-factor

There is no second factor (TOTP, WebAuthn, etc.). The `AuthBackend`
protocol in `auth.py` is the intended seam for adding one without
disturbing the rest of the system.

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
    'SELECT id, username, enc_version, length(salt_kek), length(wrapped_dek) FROM users'
# Expect: enc_version=1, salt_kek=16, wrapped_dek=60 for any user who has logged in
```

Legacy plaintext files from pre-encryption installs are migrated lazily on
the user's next successful login. Search the codebase for `LEGACY
MIGRATION` to find every transitional code path; they are paired with
`END LEGACY MIGRATION` markers and can be deleted once you have decided
all installs have upgraded.
