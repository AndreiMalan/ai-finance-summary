"""Argon2id password hashing (argon2-cffi defaults follow RFC 9106 recommendations)."""
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError

_hasher = PasswordHasher()


def hash_password(password: str) -> str:
    """Return an Argon2id hash string for `password`."""
    return _hasher.hash(password)


def verify_password(stored_hash: str, password: str) -> bool:
    """Return True when `password` matches `stored_hash`; never raises on mismatch."""
    try:
        return _hasher.verify(stored_hash, password)
    except (VerificationError, InvalidHashError):
        return False
