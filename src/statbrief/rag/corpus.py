"""The shared reference corpus (knowledge_base/): load it from its manifest and index it (owner NULL)."""
import json
from pathlib import Path

from langchain_core.documents import Document
from langchain_core.embeddings import Embeddings
from psycopg_pool import ConnectionPool

from statbrief.config import Settings
from statbrief.rag.loaders import load_reference, split_documents
from statbrief.rag.store import index_documents

MANIFEST_NAME = "manifest.json"


def load_corpus(kb_dir: Path) -> list[Document]:
    """Load every manifest document of `kb_dir` as shared, section-level Documents."""
    manifest = json.loads((kb_dir / MANIFEST_NAME).read_text(encoding="utf-8"))
    docs: list[Document] = []
    for entry in manifest["documents"]:
        docs.extend(load_reference(kb_dir / entry["file"], entry["file"], doc_id=entry["doc_id"],
                                   title=entry["title"], issuer=entry["issuer"], owner=None,
                                   source_type="reference", version=entry["version"]))
    return docs


def index_corpus(pool: ConnectionPool, settings: Settings, embeddings: Embeddings | None, kb_dir: Path) -> int:
    """Split and (re-)index the shared corpus; return the number of chunks written.

    Without embeddings the chunks are stored for full-text search only; re-running replaces them.
    """
    chunks = split_documents(load_corpus(kb_dir), settings.rag_chunk_size, settings.rag_chunk_overlap)
    return index_documents(chunks, None, embeddings, pool)
