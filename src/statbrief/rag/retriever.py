"""User-scoped retrieval of reference passages: pgvector similarity, or Postgres full-text search fallback.

Vector search is used when ENABLE_VECTOR_SEARCH is on and an embeddings model is available (D-011);
otherwise, or when no stored row has an embedding, `websearch_to_tsquery('english', ...)` full-text
search is used, relaxed to "any term" when no passage matches all terms. Every query is restricted
to shared documents plus the user's own uploads, and ties are ordered by doc_id, chunk and id.
"""
from dataclasses import dataclass

from langchain_core.documents import Document
from langchain_core.embeddings import Embeddings
from psycopg_pool import ConnectionPool

from statbrief.config import Settings
from statbrief.rag.store import CHUNK_COLUMNS, VISIBLE_TO, KbVectorStore, parse_user_id, row_to_document

_FTS_CONFIG = "english"
# Strict: every term must match (websearch syntax: quotes, OR, -negation). Relaxed: any term matches.
_STRICT_TSQUERY = f"websearch_to_tsquery('{_FTS_CONFIG}', %s)"
_RELAXED_TSQUERY = f"replace(plainto_tsquery('{_FTS_CONFIG}', %s)::text, ' & ', ' | ')::tsquery"
_FTS_QUERY = """
SELECT {columns}, ts_rank_cd(tsv, q.query) AS score
FROM kb_chunks, (SELECT {tsquery} AS query) q
WHERE numnode(q.query) > 0 AND tsv @@ q.query AND {visible}
ORDER BY score DESC, doc_id, chunk, id
LIMIT %s
"""


@dataclass(frozen=True)
class RetrievedPassage:
    """A retrieved chunk with the source metadata needed to cite it (AC 32)."""

    text: str
    doc_id: str
    title: str
    issuer: str
    section: str | None
    page: int | None
    score: float
    method: str  # "vector" | "fts"
    source_file: str
    source_type: str

    @classmethod
    def from_document(cls, doc: Document, score: float, method: str) -> "RetrievedPassage":
        """Build a passage from a store Document and its score."""
        m = doc.metadata
        return cls(text=doc.page_content, doc_id=m["doc_id"], title=m["title"], issuer=m["issuer"],
                   section=m.get("section"), page=m.get("page"), score=float(score), method=method,
                   source_file=m.get("source_file", ""), source_type=m.get("source_type", "reference"))

    def citation(self) -> str:
        """Human-readable citation: 'Title (Issuer), Section, p. N' (section/page omitted when unknown)."""
        parts = [f"{self.title} ({self.issuer})"]
        if self.section:
            parts.append(self.section)
        if self.page is not None:
            parts.append(f"p. {self.page}")
        return ", ".join(parts)


def vector_search(pool: ConnectionPool, query: str, user_id: str | None, k: int,
                  embeddings: Embeddings) -> list[RetrievedPassage]:
    """Top-k passages by cosine similarity among rows visible to the user."""
    store = KbVectorStore(pool, embeddings, user_id=user_id)
    return [RetrievedPassage.from_document(doc, score, "vector")
            for doc, score in store.similarity_search_with_score(query, k)]


def fts_search(pool: ConnectionPool, query: str, user_id: str | None, k: int) -> list[RetrievedPassage]:
    """Top-k passages by full-text rank among rows visible to the user (all terms first, then any term)."""
    owner = parse_user_id(user_id)
    with pool.connection() as c:
        for tsquery in (_STRICT_TSQUERY, _RELAXED_TSQUERY):
            sql = _FTS_QUERY.format(columns=CHUNK_COLUMNS, tsquery=tsquery, visible=VISIBLE_TO)
            rows = c.execute(sql, (query, owner, k)).fetchall()
            if rows:
                return [RetrievedPassage.from_document(row_to_document(r), r["score"], "fts") for r in rows]
    return []


def retrieve(pool: ConnectionPool, query: str, user_id: str | None, settings: Settings,
             embeddings: Embeddings | None = None, k: int | None = None) -> list[RetrievedPassage]:
    """Retrieve up to k (default RAG_TOP_K) passages visible to `user_id` (shared + own uploads).

    Vector search when enabled and `embeddings` is given; full-text search otherwise or when no
    visible row has an embedding. Blank queries return [].
    """
    k = k or settings.rag_top_k
    if not query.strip():
        return []
    if settings.enable_vector_search and embeddings is not None:
        hits = vector_search(pool, query, user_id, k, embeddings)
        if hits:
            return hits
    return fts_search(pool, query, user_id, k)
