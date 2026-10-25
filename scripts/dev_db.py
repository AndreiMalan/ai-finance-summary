"""Start a persistent embedded Postgres for local development and print DATABASE_URL.

Usage: python scripts/dev_db.py [--data-dir PATH]   (keep it running; data lives in .devdb/)
"""
import argparse
import signal
import sys
from pathlib import Path

import pgserver

DEFAULT_DATA_DIR = Path(__file__).resolve().parent.parent / ".devdb"


def parse_args() -> argparse.Namespace:
    """Parse the command line (data directory)."""
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR,
                    help=f"Postgres data directory (default: {DEFAULT_DATA_DIR})")
    return ap.parse_args()


def main() -> None:
    """Start the server in the data dir and block until Ctrl+C (or Enter on Windows)."""
    args = parse_args()
    srv = pgserver.get_server(str(args.data_dir), cleanup_mode="stop")
    print(f"DATABASE_URL={srv.get_uri()}", flush=True)
    print("Running. Press Ctrl+C to stop.", flush=True)
    try:
        signal.signal(signal.SIGINT, lambda *_: sys.exit(0))
        if hasattr(signal, "pause"):
            signal.pause()
        else:  # Windows has no signal.pause()
            input()
    except (KeyboardInterrupt, EOFError):
        pass
    finally:
        srv.cleanup()


if __name__ == "__main__":
    main()
