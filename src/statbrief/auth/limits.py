"""Login throttling and usage limits backed by Postgres (works across serverless instances)."""
import zlib
from datetime import datetime, timedelta, timezone
from typing import Literal

from psycopg_pool import ConnectionPool

KIND_REPORT_RUN = "report_run"
KIND_LLM_CALL = "llm_call"
Scope = Literal["user", "global"]


class LimitExceeded(Exception):
    """A usage limit was reached; nothing was recorded."""

    def __init__(self, kind: str, limit: int, scope: Scope):
        """Store which limit was hit for user-facing messages."""
        super().__init__(f"{scope} limit for {kind} reached ({limit})")
        self.kind, self.limit, self.scope = kind, limit, scope


def hour_ago(now: datetime) -> datetime:
    """Start of the rolling one-hour window ending at `now`."""
    return now - timedelta(hours=1)


def utc_midnight(now: datetime) -> datetime:
    """Start of the UTC calendar day containing `now`."""
    return now.astimezone(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)


def login_allowed(pool: ConnectionPool, email: str, ip: str, now: datetime,
                  max_failures: int, window: timedelta) -> bool:
    """True when this email+IP has fewer than `max_failures` failed logins within `window`."""
    with pool.connection() as c:
        row = c.execute(
            "SELECT count(*) AS n FROM login_attempts "
            "WHERE email = %s AND ip = %s AND NOT succeeded AND at > %s",
            (email.strip().lower(), ip, now - window),
        ).fetchone()
    return row["n"] < max_failures


def record_login(pool: ConnectionPool, email: str, ip: str, succeeded: bool, now: datetime) -> None:
    """Record one login attempt (no password or hash is stored)."""
    with pool.connection() as c:
        c.execute(
            "INSERT INTO login_attempts (email, ip, succeeded, at) VALUES (%s, %s, %s, %s)",
            (email.strip().lower(), ip, succeeded, now),
        )


def _count_sql(scope: Scope) -> str:
    """SQL counting events of a kind since a timestamp, per user or globally."""
    where_user = "user_id = %(uid)s AND " if scope == "user" else ""
    return f"SELECT count(*) AS n FROM usage_events WHERE {where_user}kind = %(kind)s AND at >= %(since)s"


def remaining(pool: ConnectionPool, user_id: str, kind: str, limit: int, since: datetime,
              scope: Scope = "user") -> int:
    """How many more `kind` events are allowed in the current window."""
    with pool.connection() as c:
        n = c.execute(_count_sql(scope), {"uid": user_id, "kind": kind, "since": since}).fetchone()["n"]
    return max(limit - n, 0)


def consume(pool: ConnectionPool, user_id: str, kind: str, limit: int, since: datetime,
            now: datetime, scope: Scope = "user") -> int:
    """Atomically check the limit and record one event; return remaining or raise LimitExceeded."""
    lock_key = zlib.crc32(f"{scope}:{kind}:{user_id if scope == 'user' else '*'}".encode())
    with pool.connection() as c, c.transaction():
        c.execute("SELECT pg_advisory_xact_lock(%s)", (lock_key,))
        n = c.execute(_count_sql(scope), {"uid": user_id, "kind": kind, "since": since}).fetchone()["n"]
        if n >= limit:
            raise LimitExceeded(kind, limit, scope)
        c.execute("INSERT INTO usage_events (user_id, kind, at) VALUES (%s, %s, %s)", (user_id, kind, now))
    return limit - n - 1
