"""At-rest encryption for per-user conversation data.

Two-tier key scheme:

    machine_secret ──HKDF-SHA256(salt_dek)──▶ KEK ──AES-GCM──▶ DEK
    DEK ──AES-GCM──▶ conversation files

The DEK (data key) is generated once per user at signup and stays the same
forever; it's what actually encrypts conversation blobs. The KEK (key-encrypt
key) is derived from a 32-byte `machine_secret` that lives on the server (see
`load_or_create_machine_secret`) and a per-user salt. The same KEK can be
re-derived at any time without user input, which is what lets the background
service ("midnight agent") read user data while the user is offline.

Why two tiers:
  * The DEK never changes — files written once stay readable forever.
  * Rotating `machine_secret` is a re-wrap of `wrapped_dek` per user, not a
    rewrite of every conversation file.
  * Multiple wrappings of the same DEK become possible later (e.g. a portable
    recovery passphrase, an HSM-backed second wrap) without touching files.

This module is intentionally side-effect free apart from the helper that
loads/creates `machine_secret`: no DB I/O, no path knowledge of the auth or
conversation layouts. Callers (`auth.py`, `provider_backend.py`) decide
where to store the byte blobs.

Threat model:
  * A disk-only leak that includes both the user DB and `machine_secret` can
    decrypt every user's data. See `docs/security.md` for the full picture.
  * The `machine_secret` file is 0600 and never leaves the host. The
    operational hardening path is to move it into an OS keychain or
    cloud-KMS-derived secret — that work is tracked separately.
"""

from __future__ import annotations

import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.hashes import SHA256
from cryptography.hazmat.primitives.kdf.hkdf import HKDF


# AES-GCM nonce is 12 bytes (96 bits) — the standard NIST recommendation.
# Each encryption generates a fresh random nonce; reuse would be catastrophic.
_NONCE_LEN = 12

# Length of the per-user salt for KEK derivation. 16 bytes is more than
# enough to make any kind of precomputation hopeless even across all users.
SALT_LEN = 16

# Length of the DEK we generate. 32 bytes = AES-256.
DEK_LEN = 32

# Length of the machine secret. 32 bytes of OS randomness.
MACHINE_SECRET_LEN = 32

# Length of the KEK. 32 bytes for AES-256-GCM unwrap.
_KEK_LEN = 32

# HKDF info string — binds derived keys to this purpose so the same machine
# secret used for some unrelated derivation in the future can't collide.
_HKDF_INFO = b"aime-user-dek-kek/v2"


def generate_salt() -> bytes:
    """Per-user salt for KEK derivation. Stored next to the wrapped DEK."""
    return os.urandom(SALT_LEN)


def generate_dek() -> bytes:
    """A fresh 256-bit data encryption key. Generated once per user."""
    return os.urandom(DEK_LEN)


def derive_kek(machine_secret: bytes, salt: bytes) -> bytes:
    """Derive a 32-byte KEK from the host's `machine_secret` and a per-user
    salt using HKDF-SHA256.

    HKDF (not Argon2) because `machine_secret` is 32 bytes of OS randomness —
    there is no low-entropy password to stretch. Argon2 here would only be
    cosmetic and would slow every unwrap by ~150ms for no security gain.

    Must use the same parameters every time for a given (secret, salt) or the
    KEK changes.
    """
    if len(machine_secret) != MACHINE_SECRET_LEN:
        raise ValueError(f"machine_secret must be {MACHINE_SECRET_LEN} bytes")
    if len(salt) != SALT_LEN:
        raise ValueError(f"salt must be {SALT_LEN} bytes")
    return HKDF(
        algorithm=SHA256(),
        length=_KEK_LEN,
        salt=salt,
        info=_HKDF_INFO,
    ).derive(machine_secret)


def wrap_dek(kek: bytes, dek: bytes) -> bytes:
    """Encrypt a DEK under a KEK using AES-GCM. Layout: nonce || ciphertext+tag.

    No AAD — the wrapped DEK has no contextual identifier worth binding it to.
    """
    if len(kek) != _KEK_LEN:
        raise ValueError("kek wrong length")
    nonce = os.urandom(_NONCE_LEN)
    ct = AESGCM(kek).encrypt(nonce, dek, None)
    return nonce + ct


def unwrap_dek(kek: bytes, wrapped: bytes) -> bytes:
    """Reverse of wrap_dek. Raises InvalidTag (from cryptography) on the wrong
    KEK — callers should treat this as either a corrupted row or, much more
    likely, the wrong `machine_secret` (e.g. the file was regenerated)."""
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


def load_or_create_machine_secret(path: str) -> bytes:
    """Read the host's 32-byte machine secret, generating it the first time.

    This is the root of trust for at-rest encryption: anything that can read
    this file plus the user DB can decrypt every opted-in user's data. File
    mode 0600 so only the owning user can read it. Excluded from data exports
    by design — restoring a backup to a new host requires bringing this file
    across too (or accepting that the encrypted conversations are lost).

    Re-issuing this file makes every existing `wrapped_dek` unrecoverable.
    Don't.
    """
    if os.path.exists(path):
        with open(path, "rb") as f:
            data = f.read()
        if len(data) >= MACHINE_SECRET_LEN:
            return data[:MACHINE_SECRET_LEN]
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    secret = os.urandom(MACHINE_SECRET_LEN)
    # O_CREAT|O_WRONLY|O_TRUNC with 0600 in one shot — no readable window.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, secret)
    finally:
        os.close(fd)
    return secret


def load_or_create_key_file(path: str) -> bytes:
    """Read a 32-byte DEK from disk, generating it the first time.

    Used by the local TUI, which is single-user and has no accounts database
    to look up a wrapped DEK in. The key lives on the same disk as the data
    it protects — a leaked backup that includes this file decrypts the
    conversations, same trade-off as `machine_secret` above. Web users get
    the wrapped-DEK path managed by `auth.py`.
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
