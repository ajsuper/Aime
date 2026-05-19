"""At-rest encryption for per-user conversation data.

Two-tier key scheme:

    password ──Argon2id(salt_kek)──▶ KEK ──AES-GCM──▶ DEK
    DEK ──AES-GCM──▶ conversation files

The DEK (data key) is generated once per user at signup and stays the same
forever; it's what actually encrypts conversation blobs. The KEK (key-encrypt
key) is derived from the password and only ever used to unwrap the DEK. A
password change re-derives the KEK and re-wraps the same DEK — no
conversation files need rewriting.

Why two tiers:
  * Password changes are O(1) on storage.
  * The argon2 hash used for authentication (in auth.py) and the KEK used
    here use different salts, so a leaked password_hash gives no advantage
    against the wrapped DEK and vice versa.
  * Multiple wrappings of the same DEK become possible later (recovery key,
    backup passphrase, second device) without re-encrypting files.

This module is intentionally side-effect free: no file I/O, no DB. Callers
(auth.py, provider_backend.py) decide where to store the byte blobs.
"""

from __future__ import annotations

import os

from argon2.low_level import Type, hash_secret_raw
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


# Argon2id parameters for KEK derivation. Independent of argon2-cffi's
# PasswordHasher defaults — we want raw 32-byte output, not a verifier hash.
# Memory cost in KiB; t=3 iterations; 4 lanes. Tuned for ~150ms on a modern
# laptop CPU, which is fine for an interactive login and meaningful work for
# an offline attacker.
_KEK_TIME_COST = 3
_KEK_MEMORY_COST = 64 * 1024  # 64 MiB
_KEK_PARALLELISM = 4
_KEK_LEN = 32

# AES-GCM nonce is 12 bytes (96 bits) — the standard NIST recommendation.
# Each encryption generates a fresh random nonce; reuse would be catastrophic.
_NONCE_LEN = 12

# Length of the per-user salt for KEK derivation. 16 bytes is more than
# enough to make rainbow tables hopeless even across all users globally.
SALT_LEN = 16

# Length of the DEK we generate. 32 bytes = AES-256.
DEK_LEN = 32


def generate_salt() -> bytes:
    """Per-user salt for KEK derivation. Stored next to the wrapped DEK."""
    return os.urandom(SALT_LEN)


def generate_dek() -> bytes:
    """A fresh 256-bit data encryption key. Generated once per user."""
    return os.urandom(DEK_LEN)


def derive_kek(password: str, salt: bytes) -> bytes:
    """Derive a 32-byte KEK from a password using Argon2id.

    Uses raw-output mode (not the PHC-string verifier used in auth.py), so the
    bytes can be fed straight into AES-GCM. Must use the same parameters every
    time for a given salt or the KEK changes.
    """
    if not isinstance(password, str):
        raise TypeError("password must be str")
    if len(salt) != SALT_LEN:
        raise ValueError(f"salt must be {SALT_LEN} bytes")
    return hash_secret_raw(
        secret=password.encode("utf-8"),
        salt=salt,
        time_cost=_KEK_TIME_COST,
        memory_cost=_KEK_MEMORY_COST,
        parallelism=_KEK_PARALLELISM,
        hash_len=_KEK_LEN,
        type=Type.ID,
    )


def wrap_dek(kek: bytes, dek: bytes) -> bytes:
    """Encrypt a DEK under a KEK using AES-GCM. Layout: nonce || ciphertext+tag.

    No AAD — the wrapped DEK has no contextual identifier worth binding it to,
    and adding one would only complicate password changes.
    """
    if len(kek) != _KEK_LEN:
        raise ValueError("kek wrong length")
    nonce = os.urandom(_NONCE_LEN)
    ct = AESGCM(kek).encrypt(nonce, dek, None)
    return nonce + ct


def unwrap_dek(kek: bytes, wrapped: bytes) -> bytes:
    """Reverse of wrap_dek. Raises InvalidTag (from cryptography) on the wrong
    KEK — callers should catch this and surface it as an auth failure."""
    if len(kek) != _KEK_LEN:
        raise ValueError("kek wrong length")
    if len(wrapped) < _NONCE_LEN + 16:
        raise ValueError("wrapped DEK too short")
    nonce, ct = wrapped[:_NONCE_LEN], wrapped[_NONCE_LEN:]
    return AESGCM(kek).decrypt(nonce, ct, None)


def encrypt_blob(dek: bytes, plaintext: bytes, aad: bytes) -> bytes:
    """Encrypt arbitrary bytes for at-rest storage. Layout: nonce || ct+tag.

    The aad argument binds the ciphertext to a context (e.g. the session id),
    so an attacker can't take Alice's encrypted file and drop it into Bob's
    directory under a different name — decryption fails with InvalidTag.
    """
    if len(dek) != DEK_LEN:
        raise ValueError("dek wrong length")
    nonce = os.urandom(_NONCE_LEN)
    ct = AESGCM(dek).encrypt(nonce, plaintext, aad)
    return nonce + ct


def decrypt_blob(dek: bytes, blob: bytes, aad: bytes) -> bytes:
    """Reverse of encrypt_blob. AAD must match exactly or InvalidTag is raised."""
    if len(dek) != DEK_LEN:
        raise ValueError("dek wrong length")
    if len(blob) < _NONCE_LEN + 16:
        raise ValueError("blob too short")
    nonce, ct = blob[:_NONCE_LEN], blob[_NONCE_LEN:]
    return AESGCM(dek).decrypt(nonce, ct, aad)


def load_or_create_key_file(path: str) -> bytes:
    """Read a 32-byte DEK from disk, generating it the first time.

    Used by frontends that don't have a password-derived key — chiefly the
    local TUI. The key lives on the same disk as the data it protects, so
    this protects against accidental file leaks (logs, backups copied off-
    machine) but not an attacker with disk access. Web-app users get the
    stronger password-derived KEK path in auth.py.
    """
    if os.path.exists(path):
        with open(path, "rb") as f:
            data = f.read()
        if len(data) >= DEK_LEN:
            return data[:DEK_LEN]
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    dek = generate_dek()
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, dek)
    finally:
        os.close(fd)
    return dek
