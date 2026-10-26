"""Tests for the shared HTTP helper (retries, errors, size cap) and request hashing (no network)."""
import json
from datetime import datetime, timezone

import httpx
import pytest

from statbrief.config import Settings
from statbrief.sources.http import (CachedResponse, HttpPolicy, SourceError, cached_fetch, fetch_json,
                                    request_hash)

URL = "https://api.example.test/data/x"
POLICY = HttpPolicy(timeout_s=5.0, retries=3, backoff_s=1.0, max_bytes=10_000)


def _client(handler):
    """httpx client whose requests are answered by `handler` (no network)."""
    return httpx.Client(transport=httpx.MockTransport(handler))


def _fetch(client, sleeps, policy=POLICY, params=None):
    """fetch_json with a recording sleep."""
    return fetch_json(client, URL, params or [("a", "1")], policy=policy, source="eurostat", sleep=sleeps.append)


def test_retry_on_5xx_then_success_with_exponential_backoff():
    """503, 502 → retried after 1 s, 2 s; the JSON and final URL are returned."""
    codes = iter([503, 502, 200])

    def handler(req):
        """Two server errors, then data."""
        code = next(codes)
        return httpx.Response(code, json={"ok": True} if code == 200 else {"err": "secret body"})

    sleeps: list[float] = []
    data, final_url = _fetch(_client(handler), sleeps)
    assert data == {"ok": True} and final_url == URL + "?a=1" and sleeps == [1.0, 2.0]


def test_429_is_retried():
    """Rate limiting is transient."""
    codes = iter([429, 200])
    sleeps: list[float] = []
    data, _ = _fetch(_client(lambda r: httpx.Response(next(codes), json=[1])), sleeps)
    assert data == [1] and sleeps == [1.0]


def test_404_raises_without_retry_or_body_in_message():
    """Client errors are final; the message never echoes the response body."""
    calls = []

    def handler(req):
        """Always 404 with a body."""
        calls.append(req)
        return httpx.Response(404, json={"error": "SECRET-BODY-TEXT"})

    sleeps: list[float] = []
    with pytest.raises(SourceError) as exc:
        _fetch(_client(handler), sleeps)
    assert exc.value.status == 404 and exc.value.source == "eurostat"
    assert "SECRET" not in str(exc.value) and len(calls) == 1 and sleeps == []


def test_timeout_is_retried_then_succeeds():
    """A read timeout is transient."""
    attempts = iter(["timeout", "ok"])

    def handler(req):
        """First attempt times out."""
        if next(attempts) == "timeout":
            raise httpx.ReadTimeout("slow", request=req)
        return httpx.Response(200, json={"v": 1})

    sleeps: list[float] = []
    assert _fetch(_client(handler), sleeps)[0] == {"v": 1} and sleeps == [1.0]


def test_retries_exhausted_raise_source_error():
    """After `retries` retries the last status is reported."""
    calls = []

    def handler(req):
        """Always unavailable."""
        calls.append(1)
        return httpx.Response(503)

    sleeps: list[float] = []
    with pytest.raises(SourceError) as exc:
        _fetch(_client(handler), sleeps)
    assert exc.value.status == 503 and len(calls) == 4 and sleeps == [1.0, 2.0, 4.0]


def test_timeouts_exhausted_raise_source_error_without_status():
    """Persistent timeouts end in a SourceError with no HTTP status."""
    def handler(req):
        """Always times out."""
        raise httpx.ConnectTimeout("down", request=req)

    with pytest.raises(SourceError) as exc:
        _fetch(_client(handler), [])
    assert exc.value.status is None and "timed out" in exc.value.message


def test_non_json_200_retried_then_reported():
    """An HTML maintenance page with status 200 (seen live on Eurostat) is retried, then an error."""
    def handler(req):
        """Always an HTML page."""
        return httpx.Response(200, text="<html><title>Server temporarily unavailable</title></html>")

    sleeps: list[float] = []
    with pytest.raises(SourceError) as exc:
        _fetch(_client(handler), sleeps)
    assert "not valid JSON" in exc.value.message and "<html" not in str(exc.value) and len(sleeps) == 3


def test_redirect_followed_and_final_url_returned():
    """Eurostat answers some requests with 307 to the same resource; the final URL is the query URL."""
    def handler(req):
        """Redirect once."""
        if req.url.host == "api.example.test":
            return httpx.Response(307, headers={"Location": "https://mirror.example.test/data/x?a=1"})
        return httpx.Response(200, json={"ok": 1})

    data, final_url = _fetch(_client(handler), [])
    assert data == {"ok": 1} and final_url == "https://mirror.example.test/data/x?a=1"


def test_oversized_response_rejected_without_retry():
    """Bodies above max_bytes are refused (memory guard)."""
    big = json.dumps({"v": "x" * 20_000}).encode()
    calls = []

    def handler(req):
        """A large body."""
        calls.append(1)
        return httpx.Response(200, content=big)

    with pytest.raises(SourceError) as exc:
        _fetch(_client(handler), [])
    assert "too large" in exc.value.message and len(calls) == 1


def test_request_hash_is_order_insensitive_and_scoped_by_source_and_url():
    """Same request in any param order → same hash; another URL or source → another hash."""
    h = request_hash("eurostat", URL, [("geo", "RO"), ("geo", "HU"), ("unit", "PC")])
    assert h == request_hash("eurostat", URL, [("unit", "PC"), ("geo", "HU"), ("geo", "RO")])
    assert h != request_hash("worldbank", URL, [("geo", "RO"), ("geo", "HU"), ("unit", "PC")])
    assert h != request_hash("eurostat", URL + "y", [("geo", "RO"), ("geo", "HU"), ("unit", "PC")])
    assert h != request_hash("eurostat", URL, [("geo", "RO"), ("unit", "PC")])
    assert len(h) == 64


def test_cached_fetch_without_scope_always_fetches():
    """No cache scope → fetch every time, retrieved_at from the clock (aware)."""
    calls = []
    now = datetime(2026, 10, 26, 12, tzinfo=timezone.utc)
    r = cached_fetch(None, "eurostat", URL, [], lambda: calls.append(1) or ({"v": 1}, URL), now=lambda: now)
    assert r == CachedResponse({"v": 1}, URL, now, False) and calls == [1]


def test_settings_defaults_for_api_sources():
    """API tunables live in Settings with the planned defaults."""
    s = Settings(database_url="postgresql://x/y", session_secret="s" * 32, _env_file=None)
    assert (s.api_timeout_s, s.api_retries, s.api_backoff_s) == (20.0, 3, 1.0)
    assert s.eurostat_base_url == "https://ec.europa.eu/eurostat/api/dissemination/statistics/1.0/data"
    assert s.worldbank_base_url == "https://api.worldbank.org/v2"
    p = HttpPolicy.from_settings(s)
    assert p.timeout_s == 20.0 and p.max_bytes == s.api_max_response_mb * 1024 * 1024


def test_base_urls_must_be_https():
    """A plain-http base URL is refused (provenance URLs and payloads must not be tampered with)."""
    with pytest.raises(ValueError):
        Settings(database_url="postgresql://x/y", session_secret="s" * 32, eurostat_base_url="http://x",
                 _env_file=None)
