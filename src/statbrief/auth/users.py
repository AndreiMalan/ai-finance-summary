"""User repository: creation, lookup, authentication and the env-configured demo user."""
import re
import uuid
from dataclasses import dataclass

import psycopg
from psycopg_pool import ConnectionPool

from statbrief.auth.passwords import hash_password, verify_password

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
# Verified against when the email is unknown, so timing does not reveal which emails exist.
_DUMMY_HASH = hash_password("timing-equaliser-not-a-real-password")


class InvalidEmail(ValueError):
    """The email address is syntactically invalid."""


class EmailTaken(ValueError):
    """An account with this email already exists."""


class WeakPassword(ValueError):
    """The password is shorter than the configured minimum."""


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
    if not _EMAIL_RE.match(email) or len(email) > 254:
        raise InvalidEmail("Please enter a valid email address.")
    return email


def create_user(pool: ConnectionPool, email: str, password: str, min_length: int) -> User:
    """Create a regular user; raise InvalidEmail, WeakPassword or EmailTaken."""
    email = normalize_email(email)
    if len(password) < min_length:
        raise WeakPassword(f"Password must be at least {min_length} characters.")
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


def authenticate(pool: ConnectionPool, email: str, password: str) -> User | None:
    """Return the user if email+password match, else None (constant-ish time)."""
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


def upsert_demo_user(pool: ConnectionPool, email: str, password: str) -> User:
    """Create or update the demo account so its password always equals the env value."""
    email = normalize_email(email)
    with pool.connection() as c:
        row = c.execute(
            "INSERT INTO users (email, password_hash, is_demo) VALUES (%s, %s, true) "
            "ON CONFLICT (email) DO UPDATE SET password_hash = EXCLUDED.password_hash, is_demo = true "
            "RETURNING id, email, is_demo",
            (email, hash_password(password)),
        ).fetchone()
    return _row_to_user(row)
