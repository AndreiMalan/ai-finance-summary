"""Tests for the per-report API response cache (resume never re-fetches) and its user scoping (AC 6)."""
from datetime import datetime, timezone

import pytest

from statbrief.auth.users import create_user
from statbrief.sources.http import CacheScope, cached_fetch
from statbrief.storage.db import bootstrap, create_pool
from statbrief.storage.repos import ApiCacheRepo, ReportRepo

T0 = datetime(2026, 10, 26, 9, 30, tzinfo=timezone.utc)


@pytest.fixture
def ctx(settings):
    """Pool, alice + bob, alice's report, the cache repo."""
    bootstrap(settings)
    pool = create_pool(settings)
    a = create_user(pool, "alice@example.com", "long-enough-pw", 10, 128)
    b = create_user(pool, "bob@example.com", "long-enough-pw", 10, 128)
    rep = ReportRepo(pool).create(a.id, "R")
    yield pool, a, b, str(rep["id"]), ApiCacheRepo(pool)
    pool.close()


def test_cache_round_trip(ctx):
    """A stored payload comes back with its URL and aware retrieved_at."""
    _, a, _, rid, repo = ctx
    payload = {"value": {"0": 3.1}, "id": ["geo"], "nested": [1, None, "x"]}
    repo.put(a.id, rid, "h1", "eurostat", "https://e/x?a=1", payload, T0)
    got = repo.get(a.id, rid, "h1")
    assert got is not None and got.payload == payload and got.url == "https://e/x?a=1"
    assert got.retrieved_at == T0 and got.retrieved_at.tzinfo is not None and got.source == "eurostat"
    assert repo.get(a.id, rid, "other") is None


def test_cache_is_user_scoped(ctx):
    """Bob can neither read nor write Alice's report cache; malformed ids are 'not found'."""
    _, a, b, rid, repo = ctx
    repo.put(a.id, rid, "h1", "eurostat", "https://e/x", {"v": 1}, T0)
    assert repo.get(b.id, rid, "h1") is None
    with pytest.raises(PermissionError):
        repo.put(b.id, rid, "h2", "eurostat", "https://e/x", {"v": 2}, T0)
    assert repo.get(a.id, "not-a-uuid", "h1") is None
    with pytest.raises(PermissionError):
        repo.put(a.id, "not-a-uuid", "h1", "eurostat", "https://e/x", {"v": 1}, T0)


def test_first_cached_response_wins(ctx):
    """A second put for the same request keeps the first payload (resume sees one consistent answer)."""
    _, a, _, rid, repo = ctx
    repo.put(a.id, rid, "h1", "worldbank", "https://w/1", {"v": 1}, T0)
    repo.put(a.id, rid, "h1", "worldbank", "https://w/2", {"v": 2}, T0)
    assert repo.get(a.id, rid, "h1").payload == {"v": 1}


def test_cache_rows_deleted_with_report(ctx):
    """Deleting the report cascades to its cache."""
    pool, a, _, rid, repo = ctx
    repo.put(a.id, rid, "h1", "eurostat", "https://e/x", {"v": 1}, T0)
    with pool.connection() as c:
        c.execute("DELETE FROM reports WHERE id = %s", (rid,))
        assert c.execute("SELECT count(*) AS n FROM api_cache").fetchone()["n"] == 0


def test_cached_fetch_fetches_once_per_report(ctx):
    """The second identical request is answered from the cache with the original retrieved_at."""
    _, a, _, rid, repo = ctx
    scope = CacheScope(repo, a.id, rid)
    calls = []

    def fetch():
        """Count network fetches."""
        calls.append(1)
        return {"v": len(calls)}, "https://e/x?geo=RO"

    first = cached_fetch(scope, "eurostat", "https://e/x", [("geo", "RO")], fetch, now=lambda: T0)
    later = datetime(2026, 10, 27, tzinfo=timezone.utc)
    second = cached_fetch(scope, "eurostat", "https://e/x", [("geo", "RO")], fetch, now=lambda: later)
    assert calls == [1] and not first.from_cache and second.from_cache
    assert second.payload == {"v": 1} and second.retrieved_at == T0 and second.url == "https://e/x?geo=RO"
