"""User-scoped retrieval of reference passages: pgvector similarity, or Postgres full-text search fallback.

Vector search is used when ENABLE_VECTOR_SEARCH is on and an embeddings model is available (D-011);
otherwise, or when no stored row has an embedding, `websearch_to_tsquery('english', ...)` full-text
search is used, relaxed to "any term" when no passage matches all terms. Every query is restricted
to shared documents plus the user's own uploads, and ties are ordered by doc_id, chunk and id.

Merge rule in vector mode: rows stored without an embedding (e.g. uploaded while the embeddings
provider was unavailable) cannot be found by similarity, so they are also searched by full text and
their hits are interleaved with the vector hits (vector first, then alternating), truncated to k.
If the embeddings call itself fails, retrieval degrades to full-text search (method "fts_fallback").
"""
import logging
from dataclasses import dataclass, replace

from langchain_core.documents import Document
from langchain_core.embeddings import Embeddings
from psycopg_pool import ConnectionPool

from statbrief.config import Settings
from statbrief.rag.store import CHUNK_COLUMNS, VISIBLE_TO, KbVectorStore, parse_user_id, row_to_document

logger = logging.getLogger(__name__)

_FTS_CONFIG = "english"
# querytree() is 'T' for queries without a positive term (e.g. "-inflation"), which would match almost
# every row with rank 0; such queries and rank-0 rows are excluded.
# Strict: every term must match (websearch syntax: quotes, OR, -negation). Relaxed: any term matches.
_STRICT_TSQUERY = f"websearch_to_tsquery('{_FTS_CONFIG}', %s)"
_RELAXED_TSQUERY = f"replace(plainto_tsquery('{_FTS_CONFIG}', %s)::text, ' & ', ' | ')::tsquery"
_FTS_QUERY = """
SELECT {columns}, ts_rank_cd(tsv, q.query) AS score
FROM kb_chunks, (SELECT {tsquery} AS query) q
WHERE numnode(q.query) > 0 AND querytree(q.query) <> 'T' AND tsv @@ q.query
  AND ts_rank_cd(tsv, q.query) > 0 AND {visible} {extra}
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
    method: str  # "vector" | "fts" | "fts_fallback"
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


def fts_search(pool: ConnectionPool, query: str, user_id: str | None, k: int,
               unembedded_only: bool = False) -> list[RetrievedPassage]:
    """Top-k passages by full-text rank among rows visible to the user (all terms first, then any term).

    `unembedded_only` restricts the search to rows without an embedding (the vector-mode merge rule).
    """
    owner = parse_user_id(user_id)
    extra = "AND embedding IS NULL" if unembedded_only else ""
    with pool.connection() as c:
        tree = c.execute(f"SELECT querytree({_STRICT_TSQUERY}) AS tree", (query,)).fetchone()["tree"]
        if tree in ("", "T"):  # no positive term: relaxing "-inflation" to "inflation" would invert it
            return []
        for tsquery in (_STRICT_TSQUERY, _RELAXED_TSQUERY):
            sql = _FTS_QUERY.format(columns=CHUNK_COLUMNS, tsquery=tsquery, visible=VISIBLE_TO, extra=extra)
            rows = c.execute(sql, (query, owner, k)).fetchall()
            if rows:
                return [RetrievedPassage.from_document(row_to_document(r), r["score"], "fts") for r in rows]
    return []


def _interleave(first: list[RetrievedPassage], second: list[RetrievedPassage], k: int) -> list[RetrievedPassage]:
    """Alternate the two ranked lists (starting with `first`), keeping the order within each, up to k."""
    merged: list[RetrievedPassage] = []
    for i in range(max(len(first), len(second))):
        merged.extend(lst[i] for lst in (first, second) if i < len(lst))
    return merged[:k]


def retrieve(pool: ConnectionPool, query: str, user_id: str | None, settings: Settings,
             embeddings: Embeddings | None = None, k: int | None = None) -> list[RetrievedPassage]:
    """Retrieve up to k passages visible to `user_id` (shared + own uploads).

    k defaults to RAG_TOP_K and is clamped to [1, RAG_MAX_K]. Vector search when enabled and
    `embeddings` is given (merged with full-text hits on unembedded rows, see module docstring);
    full-text search otherwise or when no visible row has an embedding; full-text search marked
    "fts_fallback" when the embeddings call fails. Blank queries return [].
    """
    k = max(1, min(k or settings.rag_top_k, settings.rag_max_k))
    if not query.strip():
        return []
    if settings.enable_vector_search and embeddings is not None:
        try:
            hits = vector_search(pool, query, user_id, k, embeddings)
        except Exception as exc:  # provider/network/key failure: degrade, never fail the report
            # Only the exception type is logged: messages may echo the query text or an API key.
            logger.warning("vector search failed (%s); falling back to full-text search", type(exc).__name__)
            return [replace(h, method="fts_fallback") for h in fts_search(pool, query, user_id, k)]
        if hits:
            return _interleave(hits, fts_search(pool, query, user_id, k, unembedded_only=True), k)
    return fts_search(pool, query, user_id, k)
