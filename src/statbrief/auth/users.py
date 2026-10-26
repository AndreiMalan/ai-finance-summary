"""User repository: creation, lookup, authentication and the env-configured demo user."""
import re
import uuid
from dataclasses import dataclass

import psycopg
from psycopg_pool import ConnectionPool

from statbrief.auth.passwords import hash_password, verify_password

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
# RFC 5321 limits a forward-path to 256 octets incl. angle brackets, i.e. 254 for the address.
_MAX_EMAIL_LENGTH = 254
MAX_EMAIL_LENGTH = _MAX_EMAIL_LENGTH  # public alias for callers that must cap email input
# Verified against when the email is unknown, so timing does not reveal which emails exist.
_DUMMY_HASH = hash_password("timing-equaliser-not-a-real-password")


class InvalidEmail(ValueError):
    """The email address is syntactically invalid."""


class EmailTaken(ValueError):
    """An account with this email already exists."""


class WeakPassword(ValueError):
    """The password is shorter than the configured minimum or longer than the maximum."""


class DemoEmailConflict(ValueError):
    """The configured demo email already belongs to a regular (non-demo) account."""


@dataclass(frozen=True)
class User:
    """An authenticated application user."""

    id: str
    email: str
    is_demo: bool


def _row_to_user(row: dict) -> User:
    """Map a `users` row to a `User`."""
    return User(id=str(row["id"]), email=row["email"], is_demo=row["is_demo"])


def normalize_email(raw: str) -> str:
    """Trim and lower-case an email; raise InvalidEmail if it is not plausible."""
    email = raw.strip().lower()
    if not _EMAIL_RE.match(email) or len(email) > _MAX_EMAIL_LENGTH:
        raise InvalidEmail("Please enter a valid email address.")
    return email


def _check_length(password: str, min_length: int, max_length: int) -> None:
    """Raise WeakPassword if `password` is shorter than `min_length` or longer than `max_length`."""
    if len(password) < min_length:
        raise WeakPassword(f"Password must be at least {min_length} characters.")
    if len(password) > max_length:
        raise WeakPassword(f"Password must be at most {max_length} characters.")


def create_user(
    pool: ConnectionPool, email: str, password: str, min_length: int, max_length: int
) -> User:
    """Create a regular user; raise InvalidEmail, WeakPassword or EmailTaken."""
    email = normalize_email(email)
    _check_length(password, min_length, max_length)
    try:
        with pool.connection() as c:
            row = c.execute(
                "INSERT INTO users (email, password_hash) VALUES (%s, %s) RETURNING id, email, is_demo",
                (email, hash_password(password)),
            ).fetchone()
    except psycopg.errors.UniqueViolation as exc:
        raise EmailTaken("An account with this email already exists.") from exc
    return _row_to_user(row)


def get_user(pool: ConnectionPool, user_id: str) -> User | None:
    """Return the user with `user_id`, or None (also for malformed ids)."""
    try:
        uid = uuid.UUID(user_id)
    except (ValueError, TypeError):
        return None
    with pool.connection() as c:
        row = c.execute("SELECT id, email, is_demo FROM users WHERE id = %s", (uid,)).fetchone()
    return _row_to_user(row) if row else None


def authenticate(pool: ConnectionPool, email: str, password: str, max_length: int) -> User | None:
    """Return the user if email+password match, else None (constant-ish time).

    Passwords longer than `max_length` are rejected without hashing, so huge
    inputs cannot be used to burn CPU; no account can have such a password.
    """
    if len(password) > max_length:
        return None
    try:
        email = normalize_email(email)
    except InvalidEmail:
        verify_password(_DUMMY_HASH, password)
        return None
    with pool.connection() as c:
        row = c.execute(
            "SELECT id, email, is_demo, password_hash FROM users WHERE email = %s", (email,)
        ).fetchone()
    if row is None:
        verify_password(_DUMMY_HASH, password)
        return None
    return _row_to_user(row) if verify_password(row["password_hash"], password) else None


def upsert_demo_user(
    pool: ConnectionPool, email: str, password: str, min_length: int, max_length: int
) -> User:
    """Create or update the demo account so its password always equals the env value.

    Raises InvalidEmail, WeakPassword (outside [min_length, max_length]), or
    DemoEmailConflict when the email already belongs to a regular account (which is
    never modified).
    """
    email = normalize_email(email)
    _check_length(password, min_length, max_length)
    with pool.connection() as c:
        row = c.execute(
            "INSERT INTO users (email, password_hash, is_demo) VALUES (%s, %s, true) "
            "ON CONFLICT (email) DO UPDATE SET password_hash = EXCLUDED.password_hash "
            "WHERE users.is_demo "
            "RETURNING id, email, is_demo",
            (email, hash_password(password)),
        ).fetchone()
    if row is None:
        raise DemoEmailConflict(
            "DEMO_USER_EMAIL belongs to a regular account; choose another address"
        )
    return _row_to_user(row)
