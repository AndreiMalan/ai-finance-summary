"""Shared HTTP access for the public APIs: JSON GET with retries, size cap, and per-report response caching.

`fetch_json` retries transient failures — HTTP 429 and 5xx, timeouts and other transport errors, and a 200
whose body is not JSON (Eurostat occasionally serves an HTML "temporarily unavailable" page with 200) —
with exponential backoff (`backoff_s · 2^attempt`), or the server's `Retry-After` (seconds) when given.
Other statuses fail at once. Failures raise `SourceError`, whose message never contains the response body
(it may echo user input or be large). Redirects are followed only within the requested URL's scheme and
host (anything else is a SourceError); the final URL is returned as the query URL for provenance.

Every fetch runs under a total time budget (`API_TOTAL_BUDGET_S`, shared by all retries and, for the
World Bank, all pages): each attempt's timeout is capped by the remaining budget, and a wait (backoff or
Retry-After) that would not fit in it ends the fetch with a "time budget" SourceError instead.

`cached_fetch` stores each raw response per report in `api_cache` (key: `request_hash`), so resuming a
report never re-fetches and always sees the same data and `retrieved_at`.
"""
import hashlib
import json
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import httpx

from statbrief.config import Settings

Params = Sequence[tuple[str, str]]
_RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
_MB = 1024 * 1024


class SourceError(Exception):
    """A public API request failed (status None when no HTTP response was received)."""

    def __init__(self, source: str, status: int | None, message: str):
        """Store the source name, HTTP status and a body-free message."""
        super().__init__(f"{source}: {message}" + (f" (HTTP {status})" if status is not None else ""))
        self.source = source
        self.status = status
        self.message = message


@dataclass(frozen=True)
class HttpPolicy:
    """Timeout, retry and size limits for one API request."""

    timeout_s: float
    retries: int
    backoff_s: float
    max_bytes: int
    total_budget_s: float | None = None  # None: no overall limit (per-attempt timeouts only)

    @classmethod
    def from_settings(cls, settings: Settings) -> "HttpPolicy":
        """Policy from API_TIMEOUT_S, API_RETRIES, API_BACKOFF_S, API_MAX_RESPONSE_MB, API_TOTAL_BUDGET_S."""
        return cls(settings.api_timeout_s, settings.api_retries, settings.api_backoff_s,
                   settings.api_max_response_mb * _MB, settings.api_total_budget_s)


class Budget:
    """Wall-clock budget shared by every attempt (and page) of one fetch; unlimited when total_s is None."""

    def __init__(self, total_s: float | None, *, clock: Callable[[], float] = time.monotonic):
        """Start the budget now on `clock`."""
        self.clock = clock
        self.deadline = None if total_s is None else clock() + total_s

    def remaining(self) -> float:
        """Seconds left (infinite without a limit)."""
        return float("inf") if self.deadline is None else self.deadline - self.clock()


class _Transient(Exception):
    """Internal: a retryable failure (status None for transport errors), with the server's Retry-After."""

    def __init__(self, status: int | None, message: str, retry_after: float | None = None):
        """Store the status, message and requested wait for the final SourceError."""
        super().__init__(message)
        self.status = status
        self.message = message
        self.retry_after = retry_after


def _retry_after(resp: httpx.Response) -> float | None:
    """Retry-After in seconds when given as a non-negative number (HTTP-date forms are ignored)."""
    value = resp.headers.get("retry-after", "").strip()
    try:
        seconds = float(value)
    except ValueError:
        return None
    return seconds if seconds >= 0 else None


def _same_origin(a: httpx.URL, b: httpx.URL) -> bool:
    """True when two URLs share scheme, host and port."""
    return (a.scheme, a.host, a.port) == (b.scheme, b.host, b.port)


def _read_capped(resp: httpx.Response, source: str, max_bytes: int) -> bytes:
    """Read a streamed body, refusing more than max_bytes (SourceError, not retried)."""
    declared = resp.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > max_bytes:
        raise SourceError(source, resp.status_code, "response too large")
    chunks, size = [], 0
    for chunk in resp.iter_bytes():
        size += len(chunk)
        if size > max_bytes:
            raise SourceError(source, resp.status_code, "response too large")
        chunks.append(chunk)
    return b"".join(chunks)


def _attempt(client: httpx.Client, url: str, params: Params, policy: HttpPolicy, source: str,
             timeout_s: float) -> tuple[Any, str]:
    """One GET: (json, final_url); _Transient for retryable failures, SourceError for final ones."""
    try:
        with client.stream("GET", url, params=list(params), timeout=timeout_s, follow_redirects=True) as resp:
            if not _same_origin(resp.url, httpx.URL(url)):
                raise SourceError(source, None, "redirected to another host or scheme")
            if resp.status_code in _RETRY_STATUSES:
                raise _Transient(resp.status_code, "temporarily unavailable", _retry_after(resp))
            if resp.status_code != 200:
                raise SourceError(source, resp.status_code, "request rejected")
            body = _read_capped(resp, source, policy.max_bytes)
            final_url = str(resp.url)
    except httpx.TimeoutException as exc:
        raise _Transient(None, "request timed out") from exc
    except httpx.TransportError as exc:
        raise _Transient(None, "connection failed") from exc
    try:
        return json.loads(body), final_url
    except ValueError as exc:
        raise _Transient(200, "response is not valid JSON") from exc


def fetch_json(client: httpx.Client, url: str, params: Params, *, policy: HttpPolicy, source: str,
               sleep: Callable[[float], None] = time.sleep, budget: Budget | None = None) -> tuple[Any, str]:
    """GET `url` with `params` and return (parsed JSON, final URL), retrying transient failures.

    `budget` is shared across calls (e.g. pages); without one, a fresh budget of `policy.total_budget_s`
    is used. Raises SourceError after `policy.retries` retries, when the budget runs out, or immediately
    for non-retryable statuses, off-host redirects and oversized bodies.
    """
    budget = budget or Budget(policy.total_budget_s)
    for attempt in range(policy.retries + 1):
        remaining = budget.remaining()
        if remaining <= 0:
            raise SourceError(source, None, "time budget exhausted")
        try:
            return _attempt(client, url, params, policy, source, min(policy.timeout_s, remaining))
        except _Transient as exc:
            if attempt == policy.retries:
                raise SourceError(source, exc.status, exc.message) from exc
            delay = exc.retry_after if exc.retry_after is not None else policy.backoff_s * 2 ** attempt
            if delay >= budget.remaining():
                raise SourceError(source, exc.status, f"{exc.message}; time budget exhausted") from exc
            sleep(delay)
    raise AssertionError("unreachable")  # pragma: no cover - the loop always returns or raises


def request_hash(source: str, url: str, params: Params) -> str:
    """sha256 over source, URL (the Eurostat dataset is in the path) and the sorted params."""
    canonical = json.dumps([source, url, sorted([str(k), str(v)] for k, v in params)], separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class CachedResponse:
    """A raw API response with the URL it came from and when it was retrieved."""

    payload: Any
    url: str
    retrieved_at: datetime
    from_cache: bool


@dataclass(frozen=True)
class CacheScope:
    """Which report's cache to use, on behalf of which user (repo: `storage.repos.ApiCacheRepo`)."""

    repo: Any
    user_id: str
    report_id: str


def utc_now() -> datetime:
    """Current time in UTC (aware)."""
    return datetime.now(timezone.utc)


def cached_fetch(scope: CacheScope | None, source: str, url: str, params: Params,
                 fetch: Callable[[], tuple[Any, str]], *, now: Callable[[], datetime] = utc_now) -> CachedResponse:
    """Return the report's cached response for this request, or call `fetch` and cache its result.

    Without a scope nothing is cached. `fetch` returns (payload, final_url) and may raise SourceError,
    in which case nothing is stored.
    """
    key = request_hash(source, url, params)
    if scope is not None:
        hit = scope.repo.get(scope.user_id, scope.report_id, key)
        if hit is not None:
            return CachedResponse(hit.payload, hit.url, hit.retrieved_at, True)
    payload, final_url = fetch()
    retrieved_at = now()
    if scope is not None:
        scope.repo.put(scope.user_id, scope.report_id, key, source, final_url, payload, retrieved_at)
        stored = scope.repo.get(scope.user_id, scope.report_id, key)  # a concurrent fetch may have won
        if stored is not None:
            return CachedResponse(stored.payload, stored.url, stored.retrieved_at, False)
    return CachedResponse(payload, final_url, retrieved_at, False)
