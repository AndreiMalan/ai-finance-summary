"""Tests for password hashing and the user repository."""
import pytest

from statbrief.auth.passwords import hash_password, verify_password
from statbrief.auth.users import (
    EmailTaken, InvalidEmail, WeakPassword,
    authenticate, create_user, get_user, normalize_email, upsert_demo_user,
)
from statbrief.storage.db import bootstrap, create_pool


@pytest.fixture
def pool(settings):
    """Migrated pool for one test."""
    bootstrap(settings)
    p = create_pool(settings)
    yield p
    p.close()


def test_hash_roundtrip_and_no_plaintext():
    """Hashes verify, reject wrong passwords, and never contain the password."""
    h = hash_password("correct horse battery")
    assert h.startswith("$argon2")
    assert "correct horse" not in h
    assert verify_password(h, "correct horse battery")
    assert not verify_password(h, "wrong")


def test_normalize_email():
    """Emails are trimmed and lower-cased; garbage is rejected."""
    assert normalize_email("  Ana@Example.COM ") == "ana@example.com"
    with pytest.raises(InvalidEmail):
        normalize_email("not-an-email")


def test_create_and_authenticate(pool):
    """A created user can log in with the right password only."""
    u = create_user(pool, "Ana@example.com", "long-enough-pw", min_length=10)
    assert u.email == "ana@example.com" and not u.is_demo
    assert authenticate(pool, "ana@example.com", "long-enough-pw") == u
    assert authenticate(pool, "ana@example.com", "nope-nope-nope") is None
    assert authenticate(pool, "ghost@example.com", "long-enough-pw") is None
    assert get_user(pool, u.id) == u


def test_duplicate_and_weak(pool):
    """Duplicate emails and short passwords are refused."""
    create_user(pool, "a@example.com", "long-enough-pw", min_length=10)
    with pytest.raises(EmailTaken):
        create_user(pool, "A@example.com", "long-enough-pw", min_length=10)
    with pytest.raises(WeakPassword):
        create_user(pool, "b@example.com", "short", min_length=10)


def test_demo_user_upsert_updates_password(pool):
    """Re-running demo upsert with a new password replaces the old one."""
    d1 = upsert_demo_user(pool, "demo@example.com", "first-password-1")
    d2 = upsert_demo_user(pool, "demo@example.com", "second-password-2")
    assert d1.id == d2.id and d2.is_demo
    assert authenticate(pool, "demo@example.com", "first-password-1") is None
    assert authenticate(pool, "demo@example.com", "second-password-2") == d2


def test_get_user_with_malformed_id_returns_none(pool):
    """A tampered/non-UUID id does not raise."""
    assert get_user(pool, "not-a-uuid") is None
