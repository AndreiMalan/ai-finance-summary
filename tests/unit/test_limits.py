"""Tests for login throttling, per-user run limits and the global daily LLM cap."""
from datetime import datetime, timedelta, timezone

import pytest

from statbrief.auth.limits import (
    KIND_LLM_CALL, KIND_REPORT_RUN, LimitExceeded,
    consume, hour_ago, login_allowed, record_login, remaining, utc_midnight,
)
from statbrief.auth.users import create_user
from statbrief.storage.db import bootstrap, create_pool

NOW = datetime(2026, 10, 25, 12, 0, tzinfo=timezone.utc)
WINDOW = timedelta(minutes=15)


@pytest.fixture
def pool(settings):
    """Migrated pool for one test."""
    bootstrap(settings)
    p = create_pool(settings)
    yield p
    p.close()


@pytest.fixture
def two_users(pool):
    """Two regular users."""
    return (create_user(pool, "a@example.com", "long-enough-pw", 10, 128),
            create_user(pool, "b@example.com", "long-enough-pw", 10, 128))


def test_login_blocked_after_max_failures_then_window_expires(pool):
    """5 failures block; failures older than the window no longer count."""
    for i in range(5):
        assert login_allowed(pool, "a@example.com", "1.1.1.1", NOW, 5, WINDOW)
        record_login(pool, "a@example.com", "1.1.1.1", False, NOW - timedelta(minutes=10) + timedelta(seconds=i))
    assert not login_allowed(pool, "a@example.com", "1.1.1.1", NOW, 5, WINDOW)
    assert login_allowed(pool, "a@example.com", "2.2.2.2", NOW, 5, WINDOW)  # other IP
    assert login_allowed(pool, "a@example.com", "1.1.1.1", NOW + timedelta(minutes=6), 5, WINDOW)


def test_user_run_limit(pool, two_users):
    """Per-user limit counts only that user's events in the window."""
    a, b = two_users
    since = hour_ago(NOW)
    assert consume(pool, a.id, KIND_REPORT_RUN, 2, since, NOW) == 1
    assert consume(pool, a.id, KIND_REPORT_RUN, 2, since, NOW) == 0
    with pytest.raises(LimitExceeded) as exc:
        consume(pool, a.id, KIND_REPORT_RUN, 2, since, NOW)
    assert exc.value.scope == "user" and exc.value.limit == 2
    assert consume(pool, b.id, KIND_REPORT_RUN, 2, since, NOW) == 1
    assert remaining(pool, a.id, KIND_REPORT_RUN, 2, since) == 0


def test_old_events_fall_out_of_window(pool, two_users):
    """Events before `since` do not count."""
    a, _ = two_users
    consume(pool, a.id, KIND_REPORT_RUN, 1, hour_ago(NOW), NOW - timedelta(hours=2))
    assert consume(pool, a.id, KIND_REPORT_RUN, 1, hour_ago(NOW), NOW) == 0


def test_global_daily_cap(pool, two_users):
    """The daily LLM cap is shared by all users and resets at UTC midnight."""
    a, b = two_users
    since = utc_midnight(NOW)
    consume(pool, a.id, KIND_LLM_CALL, 2, since, NOW, scope="global")
    consume(pool, b.id, KIND_LLM_CALL, 2, since, NOW, scope="global")
    with pytest.raises(LimitExceeded) as exc:
        consume(pool, a.id, KIND_LLM_CALL, 2, since, NOW, scope="global")
    assert exc.value.scope == "global"
    tomorrow = NOW + timedelta(days=1)
    assert consume(pool, a.id, KIND_LLM_CALL, 2, utc_midnight(tomorrow), tomorrow, scope="global") == 1


def test_window_helpers():
    """Helpers compute the expected boundaries."""
    assert hour_ago(NOW) == NOW - timedelta(hours=1)
    assert utc_midnight(NOW) == datetime(2026, 10, 25, tzinfo=timezone.utc)
