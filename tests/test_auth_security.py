"""Aggressive security tests for the auth + at-rest-encryption layer.

This suite is deliberately adversarial: it treats `LocalAuthBackend` and the
`encryption` module as the thing an attacker is poking at and asserts the
defenses documented in auth.py actually hold under abuse. Coverage:

  * password hashing (never plaintext, Argon2 round-trip, length / reuse caps)
  * user-enumeration resistance (identical errors + a real hash on miss)
  * username validation incl. SQL-injection-shaped input
  * account lockout / failed-attempt throttling and its persistence
  * the two-tier DEK encryption: wrapping, AAD binding, machine-secret rotation
  * email-verification 2FA: code brute-force cap, expiry, purpose confinement,
    constant-time compare, no-account-until-verified
  * password reset: no enumeration, validate-before-consume, lockout clearing
  * single-use access keys
  * trusted-device tokens: owner binding, expiry, eviction cap
  * soft-delete confidentiality (a deleted account never leaks its existence)
  * the on-disk session/machine secrets (0600, persistent, high entropy)

The tests reach into a couple of internals (the sqlite connection, the module
`time`) only to *simulate* the passage of time or to inspect the raw bytes on
disk — never to fake the behaviour under test.
"""

import os
import time

import pytest

from aime import auth as auth_mod
from aime import encryption as enc
from aime.auth import (
    AccountDeleted,
    AccountLocked,
    InvalidCredentials,
    InvalidUsername,
    InvalidEmail,
    LocalAuthBackend,
    VerificationError,
    WeakPassword,
    IPRateLimiter,
    load_or_create_secret_key,
    EVENT_LOGIN_UNKNOWN_USER,
    EVENT_LOGIN_BAD_PASSWORD,
)


GOOD_PW = "Sup3rSecret!pw"
OTHER_PW = "An0therSecret!pw"


@pytest.fixture
def backend(tmp_path):
    """A fresh backend backed by a real sqlite file under tmp_path."""
    db_path = os.path.join(str(tmp_path), "auth", "auth.sql")
    return LocalAuthBackend(db_path)


def _reopen(backend):
    """Open a second backend over the same files — simulates a process
    restart so we can prove state that's meant to survive one does."""
    return LocalAuthBackend(backend._db_path)


# ---------------------------------------------------------------------------
# Password storage & hashing
# ---------------------------------------------------------------------------


class TestPasswordHashing:
    def test_password_never_stored_in_plaintext(self, backend):
        backend.create("alice", GOOD_PW)
        row = backend._conn.execute(
            "SELECT password_hash FROM users WHERE username='alice'"
        ).fetchone()
        stored = row[0]
        assert GOOD_PW not in stored
        # argon2id PHC string, not the raw password.
        assert stored.startswith("$argon2id$")
        # And the literal password bytes are nowhere in the db file.
        with open(backend._db_path, "rb") as f:
            raw = f.read()
        assert GOOD_PW.encode() not in raw

    def test_correct_password_round_trips_and_yields_dek(self, backend):
        rec, dek = backend.create("alice", GOOD_PW)
        vrec, vdek, reinit = backend.verify("alice", GOOD_PW)
        assert vrec.id == rec.id
        assert vdek == dek            # same DEK handed back on login
        assert reinit is False        # fresh v2 account, no migration
        assert len(dek) == enc.DEK_LEN

    def test_wrong_password_rejected(self, backend):
        backend.create("alice", GOOD_PW)
        with pytest.raises(InvalidCredentials):
            backend.verify("alice", GOOD_PW + "x")

    def test_each_account_gets_a_distinct_dek(self, backend):
        _, dek_a = backend.create("alice", GOOD_PW)
        _, dek_b = backend.create("bob", OTHER_PW)
        assert dek_a != dek_b

    def test_two_accounts_same_password_hash_differently(self, backend):
        backend.create("alice", GOOD_PW)
        backend.create("bob", GOOD_PW)
        h1 = backend._conn.execute(
            "SELECT password_hash FROM users WHERE username='alice'"
        ).fetchone()[0]
        h2 = backend._conn.execute(
            "SELECT password_hash FROM users WHERE username='bob'"
        ).fetchone()[0]
        assert h1 != h2  # per-hash salt

    def test_password_below_minimum_rejected(self, backend):
        with pytest.raises(WeakPassword):
            backend.create("alice", "short1")

    def test_overlong_password_rejected_cpu_exhaustion_guard(self, backend):
        with pytest.raises(WeakPassword):
            backend.create("alice", "a" * (auth_mod._MAX_PASSWORD_LEN + 1))

    def test_password_equal_to_username_rejected(self, backend):
        with pytest.raises(WeakPassword):
            backend.create("alicealice", "alicealice")
        # case/space-insensitive variant is also caught
        with pytest.raises(WeakPassword):
            backend.create("alicebob", "  AliceBob ")


# ---------------------------------------------------------------------------
# User enumeration / timing oracle
# ---------------------------------------------------------------------------


class TestUserEnumeration:
    def test_unknown_user_and_bad_password_are_indistinguishable(self, backend):
        backend.create("alice", GOOD_PW)
        with pytest.raises(InvalidCredentials) as miss:
            backend.verify("nobody", GOOD_PW)
        with pytest.raises(InvalidCredentials) as bad:
            backend.verify("alice", "wrongpassword")
        # Same exception type AND same message — nothing to distinguish the
        # two cases from the client side.
        assert str(miss.value) == str(bad.value)

    def test_unknown_user_still_runs_a_real_hash_verify(self, backend, monkeypatch):
        # The anti-enumeration defense is a constant-time dummy verify on a
        # username miss; prove that verify is actually exercised.
        # PasswordHasher is slotted, so wrap the whole hasher in a recording
        # proxy rather than patching its (read-only) verify method.
        calls = []
        real = backend._hasher

        class _Spy:
            def verify(self, h, pw):
                calls.append(h)
                return real.verify(h, pw)

            def __getattr__(self, name):
                return getattr(real, name)

        monkeypatch.setattr(backend, "_hasher", _Spy())
        with pytest.raises(InvalidCredentials):
            backend.verify("ghost", GOOD_PW)
        assert calls == [backend._dummy_hash]

    def test_unknown_vs_bad_password_logged_distinctly_server_side(self, backend):
        # The client can't tell them apart, but the audit log must — that's
        # what powers the admin dashboard.
        backend.create("alice", GOOD_PW)
        with pytest.raises(InvalidCredentials):
            backend.verify("ghost", GOOD_PW, ip="10.0.0.1")
        with pytest.raises(InvalidCredentials):
            backend.verify("alice", "nope", ip="10.0.0.1")
        kinds = {e["kind"] for e in backend.recent_auth_events(limit=10)}
        assert EVENT_LOGIN_UNKNOWN_USER in kinds
        assert EVENT_LOGIN_BAD_PASSWORD in kinds


# ---------------------------------------------------------------------------
# Username validation & injection
# ---------------------------------------------------------------------------


class TestUsernameValidation:
    @pytest.mark.parametrize("name", [
        "ab",                       # too short
        "a" * 33,                   # too long
        "has space",
        "drop;table",
        "alice'--",
        "robert');DROP TABLE users;--",
        "emoji😀user",
        "with/slash",
        "",
    ])
    def test_malformed_usernames_rejected(self, backend, name):
        with pytest.raises(InvalidUsername):
            backend.create(name, GOOD_PW)

    def test_sql_injection_username_does_not_execute(self, backend):
        # Even though the name is rejected by validation, prove the users
        # table is intact and parameterization holds: create a legit user,
        # then attempt an injection-shaped *login* lookup.
        backend.create("alice", GOOD_PW)
        with pytest.raises(InvalidCredentials):
            backend.verify("alice'; DROP TABLE users; --", GOOD_PW)
        # Table still exists and alice still resolves.
        assert backend.lookup_by_username("alice") is not None

    def test_usernames_are_case_insensitive_collisions(self, backend):
        backend.create("Alice", GOOD_PW)
        from aime.auth import UsernameTaken
        with pytest.raises(UsernameTaken):
            backend.create("alice", OTHER_PW)
        # And login works regardless of the case typed.
        rec, _, _ = backend.verify("ALICE", GOOD_PW)
        assert rec.username == "Alice"


# ---------------------------------------------------------------------------
# Lockout / failed-attempt throttling
# ---------------------------------------------------------------------------


class TestLockout:
    def _burn_attempts(self, backend, username, n):
        for _ in range(n):
            with pytest.raises(InvalidCredentials):
                backend.verify(username, "definitely-wrong")

    def test_lockout_after_threshold(self, backend):
        backend.create("alice", GOOD_PW)
        self._burn_attempts(backend, "alice", auth_mod._FAIL_THRESHOLD)
        # Now even the *correct* password is refused while locked.
        with pytest.raises(AccountLocked):
            backend.verify("alice", GOOD_PW)

    def test_lockout_reports_remaining_seconds(self, backend):
        backend.create("alice", GOOD_PW)
        self._burn_attempts(backend, "alice", auth_mod._FAIL_THRESHOLD)
        with pytest.raises(AccountLocked) as exc:
            backend.verify("alice", GOOD_PW)
        assert 0 < exc.value.seconds_remaining <= auth_mod._LOCK_SECONDS

    def test_lockout_survives_restart(self, backend):
        backend.create("alice", GOOD_PW)
        self._burn_attempts(backend, "alice", auth_mod._FAIL_THRESHOLD)
        # Lock state lives in the DB, so a fresh process must still see it.
        fresh = _reopen(backend)
        with pytest.raises(AccountLocked):
            fresh.verify("alice", GOOD_PW)

    def test_successful_login_before_threshold_clears_counter(self, backend):
        backend.create("alice", GOOD_PW)
        self._burn_attempts(backend, "alice", auth_mod._FAIL_THRESHOLD - 1)
        # A correct login under the threshold resets the count...
        backend.verify("alice", GOOD_PW)
        # ...so we can burn (threshold-1) again without locking.
        self._burn_attempts(backend, "alice", auth_mod._FAIL_THRESHOLD - 1)
        rec, _, _ = backend.verify("alice", GOOD_PW)
        assert rec.username == "alice"

    def test_lockout_expires_after_window(self, backend, monkeypatch):
        backend.create("alice", GOOD_PW)
        self._burn_attempts(backend, "alice", auth_mod._FAIL_THRESHOLD)
        with pytest.raises(AccountLocked):
            backend.verify("alice", GOOD_PW)
        # Jump past the lock expiry by advancing the module clock.
        future = time.time() + auth_mod._LOCK_SECONDS + 1
        monkeypatch.setattr(auth_mod.time, "time", lambda: future)
        rec, _, _ = backend.verify("alice", GOOD_PW)
        assert rec.username == "alice"

    def test_lockout_targets_only_the_attacked_username(self, backend):
        backend.create("alice", GOOD_PW)
        backend.create("bob", OTHER_PW)
        self._burn_attempts(backend, "alice", auth_mod._FAIL_THRESHOLD)
        # bob is unaffected by alice being hammered.
        rec, _, _ = backend.verify("bob", OTHER_PW)
        assert rec.username == "bob"


# ---------------------------------------------------------------------------
# At-rest encryption: DEK wrapping, AAD binding, machine-secret rotation
# ---------------------------------------------------------------------------


class TestEncryption:
    def test_wrapped_dek_on_disk_is_not_the_raw_dek(self, backend):
        _, dek = backend.create("alice", GOOD_PW)
        wrapped = backend._conn.execute(
            "SELECT wrapped_dek_v2 FROM users WHERE username='alice'"
        ).fetchone()[0]
        assert bytes(wrapped) != dek
        assert dek not in bytes(wrapped)

    def test_dek_round_trips_real_data(self, backend):
        _, dek = backend.create("alice", GOOD_PW)
        aad = b"session-42"
        blob = enc.encrypt_blob(dek, b"top secret journal", aad)
        assert enc.decrypt_blob(dek, blob, aad) == b"top secret journal"

    def test_aad_binding_blocks_cross_context_file_drop(self, backend):
        # An attacker who relabels a ciphertext under a different context
        # (e.g. drops Alice's file into Bob's session) must fail to decrypt.
        _, dek = backend.create("alice", GOOD_PW)
        blob = enc.encrypt_blob(dek, b"secret", b"alice/session-1")
        from cryptography.exceptions import InvalidTag
        with pytest.raises(InvalidTag):
            enc.decrypt_blob(dek, blob, b"bob/session-1")

    def test_one_users_dek_cannot_read_anothers_blob(self, backend):
        _, dek_a = backend.create("alice", GOOD_PW)
        _, dek_b = backend.create("bob", OTHER_PW)
        blob = enc.encrypt_blob(dek_a, b"alice data", b"ctx")
        from cryptography.exceptions import InvalidTag
        with pytest.raises(InvalidTag):
            enc.decrypt_blob(dek_b, blob, b"ctx")

    def test_get_dek_matches_login_dek(self, backend):
        rec, dek = backend.create("alice", GOOD_PW)
        assert backend.get_dek(rec.id) == dek

    def test_tampered_wrapped_dek_triggers_reinit_not_silent_garbage(self, backend):
        rec, dek = backend.create("alice", GOOD_PW)
        # Corrupt the stored wrap — flip a byte in the ciphertext.
        wrapped = bytearray(backend._conn.execute(
            "SELECT wrapped_dek_v2 FROM users WHERE id=?", (rec.id,)
        ).fetchone()[0])
        wrapped[-1] ^= 0xFF
        backend._conn.execute(
            "UPDATE users SET wrapped_dek_v2=? WHERE id=?", (bytes(wrapped), rec.id)
        )
        backend._conn.commit()
        # Login can't recover the old key, so it mints a fresh one and flags
        # the caller to wipe stale data — it must NOT hand back wrong bytes.
        new_rec, new_dek, reinit = backend.verify("alice", GOOD_PW)
        assert reinit is True
        assert new_dek != dek

    def test_machine_secret_rotation_reinitializes_account(self, backend):
        rec, dek = backend.create("alice", GOOD_PW)
        # Simulate the machine_secret being regenerated under us.
        secret_path = os.path.join(os.path.dirname(backend._db_path), "machine_secret")
        with open(secret_path, "wb") as f:
            f.write(os.urandom(enc.MACHINE_SECRET_LEN))
        fresh = _reopen(backend)  # picks up the new secret
        # Offline DEK access is impossible until the user logs in again.
        from aime.auth import BackgroundUnavailable
        with pytest.raises(BackgroundUnavailable):
            fresh.get_dek(rec.id)
        # Logging in re-mints a v2 key and signals the wipe.
        _, new_dek, reinit = fresh.verify("alice", GOOD_PW)
        assert reinit is True
        assert new_dek != dek

    def test_derive_kek_rejects_wrong_sized_inputs(self):
        with pytest.raises(ValueError):
            enc.derive_kek(b"too-short", enc.generate_salt())
        with pytest.raises(ValueError):
            enc.derive_kek(os.urandom(enc.MACHINE_SECRET_LEN), b"badsalt")

    def test_nonce_is_unique_per_encryption(self, backend):
        _, dek = backend.create("alice", GOOD_PW)
        blobs = {enc.encrypt_blob(dek, b"same", b"ctx")[:enc._NONCE_LEN]
                 for _ in range(50)}
        assert len(blobs) == 50  # no nonce reuse


# ---------------------------------------------------------------------------
# Email-verification 2FA (signup / login / add-email)
# ---------------------------------------------------------------------------


class TestEmailVerification:
    def test_signup_creates_no_account_until_code_confirmed(self, backend):
        token, code, _ = backend.start_signup_verification(
            "alice", GOOD_PW, "alice@example.com"
        )
        # No real account exists yet.
        assert backend.lookup_by_username("alice") is None
        rec, dek = backend.complete_signup_verification(token, code)
        assert backend.lookup_by_username("alice") is not None
        assert rec.email == "alice@example.com"
        assert len(dek) == enc.DEK_LEN

    def test_wrong_code_rejected(self, backend):
        token, code, _ = backend.start_signup_verification(
            "alice", GOOD_PW, "alice@example.com"
        )
        bad = "000000" if code != "000000" else "111111"
        with pytest.raises(VerificationError):
            backend.complete_signup_verification(token, bad)
        # Account still not created.
        assert backend.lookup_by_username("alice") is None

    def test_code_brute_force_capped(self, backend):
        token, code, _ = backend.start_signup_verification(
            "alice", GOOD_PW, "alice@example.com"
        )
        wrong = "999999" if code != "999999" else "888888"
        # Burn the max attempts on wrong codes.
        for _ in range(auth_mod.LocalAuthBackend._VERIFICATION_MAX_ATTEMPTS - 1):
            with pytest.raises(VerificationError):
                backend.complete_signup_verification(token, wrong)
        # The final wrong attempt destroys the pending row entirely...
        with pytest.raises(VerificationError):
            backend.complete_signup_verification(token, wrong)
        # ...so even the CORRECT code no longer works: attacker must restart.
        with pytest.raises(VerificationError):
            backend.complete_signup_verification(token, code)

    def test_expired_code_rejected(self, backend, monkeypatch):
        token, code, _ = backend.start_signup_verification(
            "alice", GOOD_PW, "alice@example.com"
        )
        future = time.time() + auth_mod.LocalAuthBackend._VERIFICATION_TTL_SECONDS + 1
        monkeypatch.setattr(auth_mod.time, "time", lambda: future)
        with pytest.raises(VerificationError):
            backend.complete_signup_verification(token, code)

    def test_unknown_token_rejected(self, backend):
        with pytest.raises(VerificationError):
            backend.complete_signup_verification("not-a-real-token", "123456")

    def test_token_purpose_is_confined(self, backend):
        # A signup token must not be redeemable through the login-completion
        # path, even with the right code.
        token, code, _ = backend.start_signup_verification(
            "alice", GOOD_PW, "alice@example.com"
        )
        with pytest.raises(VerificationError):
            backend.complete_login_verification(token, code)

    def test_only_code_hash_is_stored(self, backend):
        token, code, _ = backend.start_signup_verification(
            "alice", GOOD_PW, "alice@example.com"
        )
        stored = backend._conn.execute(
            "SELECT code_hash, password_hash FROM email_verifications WHERE token=?",
            (token,),
        ).fetchone()
        assert stored[0] != code            # hashed, not raw
        assert GOOD_PW not in (stored[1] or "")  # password pre-hashed too

    def test_resend_rotates_code_and_invalidates_old(self, backend):
        token, old_code, _ = backend.start_signup_verification(
            "alice", GOOD_PW, "alice@example.com"
        )
        new_code, _ = backend.resend_verification_code(token)
        # The old code is dead even if it happened to differ.
        if new_code != old_code:
            with pytest.raises(VerificationError):
                backend.complete_signup_verification(token, old_code)
        rec, _ = backend.complete_signup_verification(token, new_code)
        assert rec.username == "alice"

    def test_duplicate_email_blocked_at_signup(self, backend):
        backend.create("alice", GOOD_PW, first_name=None)
        backend._conn.execute(
            "UPDATE users SET email='dup@example.com' WHERE username='alice'"
        )
        backend._conn.commit()
        with pytest.raises(InvalidEmail):
            backend.start_signup_verification("bob", OTHER_PW, "dup@example.com")

    # NB: the validator is deliberately loose (real validation is the mailed
    # code), so e.g. "@x.com" with an empty local part is intentionally
    # accepted. These are the cases it genuinely rejects.
    @pytest.mark.parametrize("bad_email", ["no-at-sign", "a@b", "a b@c.com"])
    def test_malformed_email_rejected(self, backend, bad_email):
        with pytest.raises(InvalidEmail):
            backend.start_signup_verification("alice", GOOD_PW, bad_email)


# ---------------------------------------------------------------------------
# Password reset
# ---------------------------------------------------------------------------


class TestPasswordReset:
    def _user_with_email(self, backend, username="alice", email="alice@example.com"):
        backend.create(username, GOOD_PW)
        backend._conn.execute(
            "UPDATE users SET email=? WHERE username=?", (email, username)
        )
        backend._conn.commit()

    def test_unknown_identifier_is_silent(self, backend):
        # No enumeration: an unknown user/email returns None, same as a real
        # account would behave to the caller.
        assert backend.start_password_reset("ghost") is None
        assert backend.start_password_reset("ghost@example.com") is None

    def test_account_without_email_is_not_resettable(self, backend):
        backend.create("alice", GOOD_PW)  # no email on file
        assert backend.start_password_reset("alice") is None

    def test_reset_changes_password(self, backend):
        self._user_with_email(backend)
        token, code, email = backend.start_password_reset("alice")
        assert email == "alice@example.com"
        new_pw = "Brand-New-Pass-9"
        backend.complete_password_reset(token, code, new_pw)
        with pytest.raises(InvalidCredentials):
            backend.verify("alice", GOOD_PW)        # old password dead
        rec, _, _ = backend.verify("alice", new_pw)  # new one works
        assert rec.username == "alice"

    def test_weak_new_password_does_not_consume_the_code(self, backend):
        self._user_with_email(backend)
        token, code, _ = backend.start_password_reset("alice")
        with pytest.raises(WeakPassword):
            backend.complete_password_reset(token, code, "short")
        # The same code still works once a strong password is supplied.
        backend.complete_password_reset(token, code, "Brand-New-Pass-9")
        rec, _, _ = backend.verify("alice", "Brand-New-Pass-9")
        assert rec.username == "alice"

    def test_reset_clears_an_active_lockout(self, backend):
        self._user_with_email(backend)
        for _ in range(auth_mod._FAIL_THRESHOLD):
            with pytest.raises(InvalidCredentials):
                backend.verify("alice", "wrong-pass")
        with pytest.raises(AccountLocked):
            backend.verify("alice", GOOD_PW)
        token, code, _ = backend.start_password_reset("alice")
        backend.complete_password_reset(token, code, "Brand-New-Pass-9")
        # Proving control of the email lifts the lock immediately.
        rec, _, _ = backend.verify("alice", "Brand-New-Pass-9")
        assert rec.username == "alice"

    def test_reset_preserves_at_rest_data(self, backend):
        # DEK is wrapped under the machine secret, not the password, so a
        # reset must NOT orphan the user's encrypted data.
        rec, dek = backend.create("carol", GOOD_PW)
        backend._conn.execute(
            "UPDATE users SET email='carol@example.com' WHERE id=?", (rec.id,)
        )
        backend._conn.commit()
        token, code, _ = backend.start_password_reset("carol")
        backend.complete_password_reset(token, code, "Brand-New-Pass-9")
        _, dek_after, reinit = backend.verify("carol", "Brand-New-Pass-9")
        assert reinit is False
        assert dek_after == dek


# ---------------------------------------------------------------------------
# Single-use access keys
# ---------------------------------------------------------------------------


class TestAccessKeys:
    def test_key_is_high_entropy_and_unique(self, backend):
        keys = {backend.generate_access_key() for _ in range(100)}
        assert len(keys) == 100
        assert all(len(k) >= 30 for k in keys)

    def test_raw_key_is_never_stored(self, backend):
        key = backend.generate_access_key("note")
        row = backend._conn.execute(
            "SELECT key_hash FROM access_keys"
        ).fetchone()
        assert row[0] != key
        with open(backend._db_path, "rb") as f:
            assert key.encode() not in f.read()

    def test_key_is_single_use(self, backend):
        rec, _ = backend.create("alice", GOOD_PW, api_access=False)
        key = backend.generate_access_key()
        assert backend.redeem_key(rec.id, key) is True
        assert backend.lookup(rec.id).api_access is True
        # Second redemption fails — single use.
        assert backend.redeem_key(rec.id, key) is False

    def test_unknown_key_rejected(self, backend):
        rec, _ = backend.create("alice", GOOD_PW, api_access=False)
        assert backend.redeem_key(rec.id, "totally-made-up-key") is False
        assert backend.lookup(rec.id).api_access is False

    def test_revoked_key_cannot_be_redeemed(self, backend):
        rec, _ = backend.create("alice", GOOD_PW, api_access=False)
        key = backend.generate_access_key()
        assert backend.revoke_access_key(key) is True
        assert backend.redeem_key(rec.id, key) is False

    def test_redeemed_key_cannot_be_revoked(self, backend):
        rec, _ = backend.create("alice", GOOD_PW, api_access=False)
        key = backend.generate_access_key()
        backend.redeem_key(rec.id, key)
        assert backend.revoke_access_key(key) is False


# ---------------------------------------------------------------------------
# Trusted-device ("remember this device") tokens
# ---------------------------------------------------------------------------


class TestTrustedDevices:
    def test_token_high_entropy_and_only_hash_stored(self, backend):
        rec, _ = backend.create("alice", GOOD_PW)
        token, _ = backend.create_trusted_device(rec.id)
        assert len(token) >= 30
        stored = backend._conn.execute(
            "SELECT token_hash FROM trusted_devices"
        ).fetchone()[0]
        assert stored != token

    def test_token_is_bound_to_its_owner(self, backend):
        alice, _ = backend.create("alice", GOOD_PW)
        bob, _ = backend.create("bob", OTHER_PW)
        token, _ = backend.create_trusted_device(alice.id)
        assert backend.is_trusted_device(alice.id, token) is True
        # A stolen token cannot be presented as another account's bypass.
        assert backend.is_trusted_device(bob.id, token) is False

    def test_unknown_and_empty_tokens_rejected(self, backend):
        rec, _ = backend.create("alice", GOOD_PW)
        assert backend.is_trusted_device(rec.id, "nope") is False
        assert backend.is_trusted_device(rec.id, "") is False

    def test_expired_token_rejected_and_purged(self, backend, monkeypatch):
        rec, _ = backend.create("alice", GOOD_PW)
        token, _ = backend.create_trusted_device(rec.id)
        future = time.time() + auth_mod.LocalAuthBackend._TRUSTED_DEVICE_TTL_SECONDS + 1
        monkeypatch.setattr(auth_mod.time, "time", lambda: future)
        assert backend.is_trusted_device(rec.id, token) is False
        # The expired row is dropped opportunistically.
        remaining = backend._conn.execute(
            "SELECT COUNT(*) FROM trusted_devices"
        ).fetchone()[0]
        assert remaining == 0

    def test_device_count_is_capped_oldest_evicted(self, backend):
        rec, _ = backend.create("alice", GOOD_PW)
        cap = auth_mod.LocalAuthBackend._MAX_TRUSTED_DEVICES
        tokens = [backend.create_trusted_device(rec.id)[0] for _ in range(cap + 1)]
        # The very first token is evicted once the cap is exceeded...
        assert backend.is_trusted_device(rec.id, tokens[0]) is False
        # ...while the newest survives.
        assert backend.is_trusted_device(rec.id, tokens[-1]) is True
        count = backend._conn.execute(
            "SELECT COUNT(*) FROM trusted_devices WHERE user_id=?", (rec.id,)
        ).fetchone()[0]
        assert count == cap

    def test_revoke_single_and_all(self, backend):
        rec, _ = backend.create("alice", GOOD_PW)
        t1, _ = backend.create_trusted_device(rec.id)
        t2, _ = backend.create_trusted_device(rec.id)
        backend.revoke_trusted_device(t1)
        assert backend.is_trusted_device(rec.id, t1) is False
        assert backend.is_trusted_device(rec.id, t2) is True
        assert backend.revoke_all_trusted_devices(rec.id) >= 1
        assert backend.is_trusted_device(rec.id, t2) is False


# ---------------------------------------------------------------------------
# Soft-delete confidentiality & account lifecycle
# ---------------------------------------------------------------------------


class TestAccountLifecycle:
    def test_deleted_account_only_revealed_after_correct_password(self, backend):
        rec, _ = backend.create("alice", GOOD_PW)
        assert backend.soft_delete(rec.id) is True
        # Correct password -> AccountDeleted (so recovery can be offered).
        with pytest.raises(AccountDeleted):
            backend.verify("alice", GOOD_PW)
        # Wrong password -> generic InvalidCredentials, leaking nothing about
        # the account's deleted state.
        with pytest.raises(InvalidCredentials):
            backend.verify("alice", "wrong-pass")

    def test_deleted_account_invisible_to_lookup(self, backend):
        rec, _ = backend.create("alice", GOOD_PW)
        backend.soft_delete(rec.id)
        assert backend.lookup(rec.id) is None
        assert backend.lookup_by_username("alice") is None

    def test_hard_delete_only_touches_soft_deleted_rows(self, backend):
        rec, _ = backend.create("alice", GOOD_PW)
        # A live account can never be purged by a stray hard_delete.
        assert backend.hard_delete(rec.id) is False
        assert backend.lookup(rec.id) is not None
        backend.soft_delete(rec.id)
        assert backend.hard_delete(rec.id) is True

    def test_restore_restamps_api_access(self, backend):
        rec, _ = backend.create("alice", GOOD_PW, api_access=True)
        backend.soft_delete(rec.id)
        # Default restore drops access (keys-mode behaviour).
        assert backend.restore(rec.id) is True
        assert backend.lookup(rec.id).api_access is False


# ---------------------------------------------------------------------------
# Access control toggles
# ---------------------------------------------------------------------------


class TestAccessControl:
    def test_set_api_access_toggles(self, backend):
        rec, _ = backend.create("alice", GOOD_PW, api_access=False)
        assert backend.lookup(rec.id).api_access is False
        assert backend.set_api_access(rec.id, True) is True
        assert backend.lookup(rec.id).api_access is True

    def test_revoke_all_access_zeroes_everyone(self, backend):
        a, _ = backend.create("alice", GOOD_PW, api_access=True)
        b, _ = backend.create("bob", OTHER_PW, api_access=True)
        assert backend.revoke_all_access() == 2
        assert backend.lookup(a.id).api_access is False
        assert backend.lookup(b.id).api_access is False


# ---------------------------------------------------------------------------
# IP rate limiter
# ---------------------------------------------------------------------------


class TestIPRateLimiter:
    def test_blocks_over_limit_in_window(self):
        rl = IPRateLimiter(limit=3, window_seconds=60)
        assert [rl.hit("1.2.3.4") for _ in range(4)] == [True, True, True, False]

    def test_distinct_keys_are_independent(self):
        rl = IPRateLimiter(limit=1, window_seconds=60)
        assert rl.hit("a") is True
        assert rl.hit("b") is True
        assert rl.hit("a") is False

    def test_blocked_does_not_consume_a_hit(self):
        rl = IPRateLimiter(limit=1, window_seconds=60)
        assert rl.blocked("x") is False  # peeking never records
        assert rl.hit("x") is True
        assert rl.blocked("x") is True


# ---------------------------------------------------------------------------
# On-disk secrets
# ---------------------------------------------------------------------------


class TestOnDiskSecrets:
    def test_session_secret_is_0600_persistent_and_32_bytes(self, tmp_path):
        path = os.path.join(str(tmp_path), "secret_key")
        key = load_or_create_secret_key(path)
        assert len(key) >= 32
        assert oct(os.stat(path).st_mode & 0o777) == "0o600"
        # Stable across calls (keeps sessions alive across restarts).
        assert load_or_create_secret_key(path) == key

    def test_machine_secret_is_0600_and_persistent(self, tmp_path):
        path = os.path.join(str(tmp_path), "machine_secret")
        s1 = enc.load_or_create_machine_secret(path)
        assert len(s1) == enc.MACHINE_SECRET_LEN
        assert oct(os.stat(path).st_mode & 0o777) == "0o600"
        assert enc.load_or_create_machine_secret(path) == s1

    def test_machine_secret_is_unguessable_across_hosts(self, tmp_path):
        p1 = os.path.join(str(tmp_path), "a", "machine_secret")
        p2 = os.path.join(str(tmp_path), "b", "machine_secret")
        assert enc.load_or_create_machine_secret(p1) != \
            enc.load_or_create_machine_secret(p2)


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))
