"""Tests for login throttling, per-user run limits and the global daily LLM cap."""
import threading
import time
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


N_THREADS = 8
CONCURRENT_LIMIT = 3
WAIT_TIMEOUT_S = 20.0
POLL_INTERVAL_S = 0.05


@pytest.fixture
def wide_pool(settings):
    """Migrated pool with room for all workers plus a lock holder and a poller."""
    s = settings.model_copy(update={"db_pool_max": N_THREADS + 3})
    bootstrap(s)
    p = create_pool(s)
    yield p
    p.close()


def _wait_for_lock_waiters(pool, expected):
    """Block until `expected` other backends in this database are waiting on a heavyweight lock."""
    deadline = time.monotonic() + WAIT_TIMEOUT_S
    with pool.connection() as c:
        while True:
            n = c.execute(
                "SELECT count(*) AS n FROM pg_stat_activity WHERE datname = current_database() "
                "AND pid <> pg_backend_pid() AND wait_event_type = 'Lock'"
            ).fetchone()["n"]
            if n >= expected:
                return
            if time.monotonic() > deadline:
                raise TimeoutError(f"only {n}/{expected} backends waiting on locks")
            time.sleep(POLL_INTERVAL_S)


def test_consume_is_atomic_under_concurrency(wide_pool):
    """Simultaneous consumers never exceed the limit: exactly `limit` succeed and are recorded.

    To force the race deterministically, the test holds SHARE on usage_events (blocks INSERT,
    allows SELECT) until every worker is blocked. Without the advisory lock all workers would
    count 0 before any insert lands and all would succeed; with it, they serialise.
    """
    a = create_user(wide_pool, "a@example.com", "long-enough-pw", 10, 128)
    since = hour_ago(NOW)
    barrier = threading.Barrier(N_THREADS)
    results, errors, lock = [], [], threading.Lock()

    def worker():
        """Wait for all threads, then try to consume once and record the outcome (or error)."""
        try:
            barrier.wait()
            try:
                consume(wide_pool, a.id, KIND_REPORT_RUN, CONCURRENT_LIMIT, since, NOW)
                outcome = "ok"
            except LimitExceeded:
                outcome = "limited"
            with lock:
                results.append(outcome)
        except BaseException as exc:  # surfaced below; a thread's exception is otherwise lost
            with lock:
                errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(N_THREADS)]
    with wide_pool.connection() as holder, holder.transaction():
        holder.execute("LOCK TABLE usage_events IN SHARE MODE")
        for t in threads:
            t.start()
        _wait_for_lock_waiters(wide_pool, N_THREADS)
    for t in threads:
        t.join(timeout=WAIT_TIMEOUT_S)
    assert not any(t.is_alive() for t in threads), "worker threads did not finish"
    assert errors == []
    assert len(results) == N_THREADS
    assert results.count("ok") == CONCURRENT_LIMIT
    assert results.count("limited") == N_THREADS - CONCURRENT_LIMIT
    with wide_pool.connection() as c:
        n = c.execute("SELECT count(*) AS n FROM usage_events WHERE user_id = %s", (a.id,)).fetchone()["n"]
    assert n == CONCURRENT_LIMIT


@pytest.mark.parametrize("bad_scope", ["users", "", "GLOBAL", None])
def test_invalid_scope_rejected(pool, two_users, bad_scope):
    """Unknown scopes raise ValueError instead of silently counting globally."""
    a, _ = two_users
    with pytest.raises(ValueError):
        consume(pool, a.id, KIND_REPORT_RUN, 1, hour_ago(NOW), NOW, scope=bad_scope)
    with pytest.raises(ValueError):
        remaining(pool, a.id, KIND_REPORT_RUN, 1, hour_ago(NOW), scope=bad_scope)


NAIVE = datetime(2026, 10, 25, 12, 0)


def test_naive_datetimes_rejected(pool, two_users):
    """Every public entry point refuses datetimes without tzinfo."""
    a, _ = two_users
    with pytest.raises(ValueError):
        hour_ago(NAIVE)
    with pytest.raises(ValueError):
        utc_midnight(NAIVE)
    with pytest.raises(ValueError):
        login_allowed(pool, "a@example.com", "1.1.1.1", NAIVE, 5, WINDOW)
    with pytest.raises(ValueError):
        record_login(pool, "a@example.com", "1.1.1.1", False, NAIVE)
    with pytest.raises(ValueError):
        consume(pool, a.id, KIND_REPORT_RUN, 1, NAIVE, NOW)
    with pytest.raises(ValueError):
        consume(pool, a.id, KIND_REPORT_RUN, 1, hour_ago(NOW), NAIVE)
    with pytest.raises(ValueError):
        remaining(pool, a.id, KIND_REPORT_RUN, 1, NAIVE)
    assert remaining(pool, a.id, KIND_REPORT_RUN, 1, hour_ago(NOW)) == 1  # nothing recorded


def test_lock_key_fits_int4_for_all_inputs():
    """The derived second lock key always fits Postgres int4, even when crc32 >= 2**31."""
    from statbrief.auth.limits import _lock_key

    keys = [_lock_key(scope, kind, f"user-{i}") for i in range(2000)
            for scope in ("user", "global") for kind in (KIND_REPORT_RUN, KIND_LLM_CALL)]
    assert all(-2**31 <= k < 2**31 for k in keys)
    assert any(k < 0 for k in keys)  # high-bit crc32 values are exercised
