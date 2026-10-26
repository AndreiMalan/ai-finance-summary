"""Index the shared reference corpus (knowledge_base/) into kb_chunks.

Usage: python scripts/index_knowledge_base.py [--kb DIR] [--fts-only]

Reads DATABASE_URL (and DATABASE_URL_UNPOOLED, OPENAI_API_KEY, ...) from the environment / .env like the
app. Runs migrations first. With an OpenAI key and ENABLE_VECTOR_SEARCH=true the chunks are embedded;
otherwise (or with --fts-only) they are stored for full-text search only. Re-running replaces the rows.
"""
import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from statbrief.config import get_settings  # noqa: E402
from statbrief.rag.corpus import index_corpus  # noqa: E402
from statbrief.rag.embeddings import make_embeddings  # noqa: E402
from statbrief.storage.db import bootstrap, create_pool  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    """Index the corpus and return the number of chunks written."""
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--kb", type=Path, default=REPO_ROOT / "knowledge_base", help="knowledge base directory")
    ap.add_argument("--fts-only", action="store_true", help="skip embeddings even when a key is configured")
    args = ap.parse_args(argv)
    settings = get_settings()
    bootstrap(settings)
    embeddings = None if args.fts_only else make_embeddings(settings)
    pool = create_pool(settings)
    try:
        written = index_corpus(pool, settings, embeddings, args.kb)
    finally:
        pool.close()
    mode = "with embeddings" if embeddings else "full-text search only (no embeddings)"
    print(f"indexed {written} chunks from {args.kb} - {mode}")
    return written


if __name__ == "__main__":
    main()
