"""
AID PLUS+ — Security Helpers
=============================
Cryptographic utilities for password hashing, ID generation,
and secure reference creation.

All functions use the `secrets` module (OS cryptographic RNG).
Never use `random` for anything security-sensitive.
"""
from __future__ import annotations
import hashlib
import secrets


def hash_password(password: str, salt: str = None) -> tuple:
    """
    Hash a password with SHA-256 + salt.
    Returns (hashed_hex, salt_hex).
    If no salt is provided a fresh 32-byte cryptographic salt is generated.
    """
    if salt is None:
        salt = secrets.token_hex(32)        # [S2] cryptographic RNG
    digest = hashlib.sha256((salt + password).encode("utf-8")).hexdigest()
    return digest, salt


def verify_password(stored_hash: str, stored_salt: str, candidate: str) -> bool:
    """Constant-time comparison to prevent timing attacks. [S1]"""
    digest, _ = hash_password(candidate, stored_salt)
    return secrets.compare_digest(digest, stored_hash)


def secure_id(digits: int = 8) -> str:
    """Cryptographically secure numeric ID string. [S2]"""
    lower = 10 ** (digits - 1)
    upper = 10 **  digits - 1
    return str(secrets.randbelow(upper - lower + 1) + lower)


def secure_code(digits: int = 6) -> str:
    """Cryptographically secure N-digit numeric token. [S2]"""
    return str(secrets.randbelow(9 * 10 ** (digits - 1)) + 10 ** (digits - 1))


def secure_ref(prefix: str = "TXN") -> str:
    """Prefixed cryptographic reference number. [S2]"""
    return f"{prefix}-{secrets.token_hex(4).upper()}"
