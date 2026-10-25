"""Verify LangGraph PostgresSaver + interrupt/resume against Postgres (risk R3, Neon pooler).

Mirrors the app: `.setup()` runs on DATABASE_URL_UNPOOLED (direct) when set, otherwise on
DATABASE_URL; the run and the resume each use a fresh pool on DATABASE_URL (pooled).

Usage: DATABASE_URL=... [DATABASE_URL_UNPOOLED=...] python scripts/check_checkpointer.py
"""
import argparse
import os
import sys
import uuid
from pathlib import Path
from typing import TypedDict
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from langgraph.checkpoint.postgres import PostgresSaver  # noqa: E402
from langgraph.graph import END, START, StateGraph  # noqa: E402
from langgraph.types import Command, interrupt  # noqa: E402
from psycopg_pool import ConnectionPool  # noqa: E402

from statbrief.config import Settings  # noqa: E402
# Same connection kwargs as the app's pool (autocommit, prepare_threshold=None, dict rows).
from statbrief.storage.db import CONN_KWARGS, connect, setup_langgraph  # noqa: E402


class S(TypedDict, total=False):
    """Minimal state for the probe graph."""

    x: int
    answer: str


def host_of(url: str) -> str:
    """host[:port]/db of a Postgres URL, without credentials or query, for printing."""
    parts = urlsplit(url)
    port = f":{parts.port}" if parts.port else ""
    return f"{parts.hostname}{port}{parts.path}"


def build(pool: ConnectionPool):
    """Compile the 2-node probe graph with a PostgresSaver on `pool` (tables already set up)."""
    g = StateGraph(S)
    g.add_node("a", lambda s: {"x": 1})
    g.add_node("b", lambda s: {"answer": interrupt("approve?")})
    g.add_edge(START, "a")
    g.add_edge("a", "b")
    g.add_edge("b", END)
    return g.compile(checkpointer=PostgresSaver(pool))


def parse_args() -> argparse.Namespace:
    """Parse the command line (setup lock timeout defaults to the app's DB_SETUP_LOCK_TIMEOUT_S)."""
    default_timeout = os.environ.get("DB_SETUP_LOCK_TIMEOUT_S",
                                     Settings.model_fields["db_setup_lock_timeout_s"].default)
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--lock-timeout-s", type=float, default=float(default_timeout))
    return ap.parse_args()


def main() -> None:
    """Set up tables directly, run a graph to an interrupt, resume it from a NEW pool, report."""
    args = parse_args()
    pooled = os.environ["DATABASE_URL"]
    unpooled = os.environ.get("DATABASE_URL_UNPOOLED")
    direct = unpooled or pooled
    note = "  (DATABASE_URL_UNPOOLED)" if unpooled else "  (DATABASE_URL_UNPOOLED not set -> DATABASE_URL)"
    print(f"setup      host: {host_of(direct)}{note}")
    print(f"run/resume host: {host_of(pooled)}")

    with connect(direct) as conn:
        setup_langgraph(conn, args.lock_timeout_s)

    cfg = {"configurable": {"thread_id": f"probe-{uuid.uuid4().hex[:8]}"}}
    with ConnectionPool(pooled, kwargs=CONN_KWARGS, open=True) as p1:
        out = build(p1).invoke({}, cfg)
        assert "__interrupt__" in out, out
        print("interrupted:", out["__interrupt__"])
    with ConnectionPool(pooled, kwargs=CONN_KWARGS, open=True) as p2:  # simulates a new serverless instance
        final = build(p2).invoke(Command(resume="yes"), cfg)
    assert final == {"x": 1, "answer": "yes"}, final
    print("R3 OK: interrupt persisted and resumed across pools:", final)


if __name__ == "__main__":
    main()
