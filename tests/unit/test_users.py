"""Tests for password hashing and the user repository."""
import uuid

import pytest

from statbrief.auth.passwords import hash_password, verify_password
from statbrief.auth.users import (
    DemoEmailConflict, EmailTaken, InvalidEmail, WeakPassword,
    authenticate, create_user, get_user, normalize_email, upsert_demo_user,
)
from statbrief.storage.db import bootstrap, create_pool

MIN = 10
MAX = 128


@pytest.fixture
def pool(settings):
    """Migrated pool for one test."""
    bootstrap(settings)
    p = create_pool(settings)
    yield p
    p.close()


def test_hash_roundtrip_and_salted():
    """Hashes verify, reject wrong passwords, and are salted (two hashes differ)."""
    h = hash_password("correct horse battery")
    assert h.startswith("$argon2id$")
    assert hash_password("correct horse battery") != h
    assert verify_password(h, "correct horse battery")
    assert not verify_password(h, "wrong")


def test_verify_password_corrupt_hash_returns_false():
    """A corrupt or non-Argon2 stored hash yields False instead of raising."""
    assert verify_password("not-a-hash", "whatever") is False
    assert verify_password("$argon2id$v=19$m=garbage", "whatever") is False


def test_normalize_email():
    """Emails are trimmed and lower-cased; garbage is rejected."""
    assert normalize_email("  Ana@Example.COM ") == "ana@example.com"
    with pytest.raises(InvalidEmail):
        normalize_email("not-an-email")
    with pytest.raises(InvalidEmail):
        normalize_email("a" * 250 + "@x.com")


def test_create_and_authenticate(pool):
    """A created user can log in with the right password only."""
    u = create_user(pool, "Ana@example.com", "long-enough-pw", min_length=MIN, max_length=MAX)
    assert u.email == "ana@example.com" and not u.is_demo
    assert authenticate(pool, "ana@example.com", "long-enough-pw", max_length=MAX) == u
    assert authenticate(pool, "ANA@Example.com", "long-enough-pw", max_length=MAX) == u
    assert authenticate(pool, "ana@example.com", "nope-nope-nope", max_length=MAX) is None
    assert authenticate(pool, "ghost@example.com", "long-enough-pw", max_length=MAX) is None
    assert authenticate(pool, "garbage", "long-enough-pw", max_length=MAX) is None
    assert get_user(pool, u.id) == u


def test_duplicate_and_weak(pool):
    """Duplicate emails and short passwords are refused."""
    create_user(pool, "a@example.com", "long-enough-pw", min_length=MIN, max_length=MAX)
    with pytest.raises(EmailTaken):
        create_user(pool, "A@example.com", "long-enough-pw", min_length=MIN, max_length=MAX)
    with pytest.raises(WeakPassword):
        create_user(pool, "b@example.com", "short", min_length=MIN, max_length=MAX)


def test_password_max_length(pool):
    """Over-long passwords are refused at signup and never authenticate."""
    with pytest.raises(WeakPassword, match="at most 128"):
        create_user(pool, "c@example.com", "x" * (MAX + 1), min_length=MIN, max_length=MAX)
    exact = "y" * MAX
    u = create_user(pool, "d@example.com", exact, min_length=MIN, max_length=MAX)
    assert authenticate(pool, "d@example.com", exact, max_length=MAX) == u
    assert authenticate(pool, "d@example.com", exact + "z", max_length=MAX) is None


def test_demo_user_upsert_updates_password(pool):
    """Re-running demo upsert with a new password replaces the old one."""
    d1 = upsert_demo_user(pool, "demo@example.com", "first-password-1", min_length=MIN, max_length=MAX)
    d2 = upsert_demo_user(pool, "demo@example.com", "second-password-2", min_length=MIN, max_length=MAX)
    assert d1.id == d2.id and d2.is_demo
    assert authenticate(pool, "demo@example.com", "first-password-1", max_length=MAX) is None
    assert authenticate(pool, "demo@example.com", "second-password-2", max_length=MAX) == d2


def test_demo_upsert_never_takes_over_regular_account(pool):
    """If the demo email belongs to a regular user, upsert refuses and leaves it intact."""
    u = create_user(pool, "taken@example.com", "regular-password", min_length=MIN, max_length=MAX)
    with pytest.raises(DemoEmailConflict):
        upsert_demo_user(pool, "Taken@example.com", "demo-password-xyz", min_length=MIN, max_length=MAX)
    assert authenticate(pool, "taken@example.com", "regular-password", max_length=MAX) == u
    assert authenticate(pool, "taken@example.com", "demo-password-xyz", max_length=MAX) is None
    assert not get_user(pool, u.id).is_demo


def test_demo_upsert_enforces_min_length(pool):
    """A too-short demo password is refused before touching the database."""
    with pytest.raises(WeakPassword):
        upsert_demo_user(pool, "demo@example.com", "short", min_length=MIN, max_length=MAX)
    assert authenticate(pool, "demo@example.com", "short", max_length=MAX) is None


def test_get_user_with_malformed_id_returns_none(pool):
    """A tampered/non-UUID id does not raise."""
    assert get_user(pool, "not-a-uuid") is None


def test_get_user_unknown_uuid_returns_none(pool):
    """A well-formed UUID with no matching row returns None."""
    assert get_user(pool, str(uuid.uuid4())) is None


def test_demo_upsert_enforces_max_length(pool):
    """A demo password above the maximum is refused like any other account's."""
    with pytest.raises(WeakPassword, match="at most"):
        upsert_demo_user(pool, "demo@example.com", "p" * (MAX + 1), min_length=MIN, max_length=MAX)
    with pool.connection() as c:
        assert c.execute("SELECT count(*) AS n FROM users").fetchone()["n"] == 0
