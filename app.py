"""Vercel entrypoint: exposes the ASGI `app` (src/ layout added to sys.path).

Vercel's Python runtime auto-detects a top-level `app` in a root-level `app.py`
(https://vercel.com/docs/functions/runtimes/python#python-entrypoints). The `statbrief`
package itself is not pip-installed on Vercel, so `src/` is put on the import path here.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))

from statbrief.web.main import create_app  # noqa: E402

app = create_app()
