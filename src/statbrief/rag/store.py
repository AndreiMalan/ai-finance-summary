"""kb_chunks storage: idempotent indexing and a user-scoped LangChain VectorStore over psycopg + pgvector.

Why not langchain-postgres (0.0.19, checked 2026-10-26)? Its legacy `PGVector` only writes to its own
`langchain_pg_collection` / `langchain_pg_embedding` tables. The newer `PGVectorStore` can target a custom
table, but it (a) runs on a separate SQLAlchemy async engine (asyncpg or async psycopg) with its own
pool and background event-loop thread next to our psycopg pool, (b) always embeds on insert, so it
cannot write the FTS-only rows (embedding NULL) needed without an API key, (c) expresses owner scoping
only through an optional, caller-supplied metadata filter dict, and (d) orders by distance only, with
no deterministic tie-break. User scoping is a security property here, so it is enforced in SQL by
this small `VectorStore` subclass instead: every query includes `owner IS NULL OR owner = <user>`.
"""
import uuid
from collections.abc import Iterable, Sequence
from typing import Any

from langchain_core.documents import Document
from langchain_core.embeddings import Embeddings
from langchain_core.vectorstores import VectorStore
from psycopg_pool import ConnectionPool

from statbrief.rag.loaders import USER_DOC_PREFIX

# HNSW + a WHERE filter can drop candidates (pgvector < 0.8 has no iterative scan), so the index scan
# fetches this many times k before the owner filter and the deterministic re-sort.
VECTOR_CANDIDATE_FACTOR = 4
CHUNK_COLUMNS = "id, doc_id, owner, issuer, title, section, page, chunk, source_file, source_type, version, text"
_INSERT = (
    "INSERT INTO kb_chunks (doc_id, owner, issuer, title, section, page, chunk, source_file, source_type, "
    "version, text, embedding) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::vector) RETURNING id"
)
# Rows visible to a user: shared (owner NULL) plus their own; a NULL user sees shared rows only.
VISIBLE_TO = "(owner IS NULL OR owner = %s)"
_VECTOR_QUERY = f"""
SELECT * FROM (
    SELECT {CHUNK_COLUMNS}, embedding <=> %s::vector AS distance
    FROM kb_chunks
    WHERE embedding IS NOT NULL AND {VISIBLE_TO}
    ORDER BY embedding <=> %s::vector
    LIMIT %s
) candidates
ORDER BY distance, doc_id, chunk, id
LIMIT %s
"""


def parse_user_id(user_id: str | None) -> uuid.UUID | None:
    """A user id as UUID; None for None or malformed ids (which then see shared documents only)."""
    if user_id is None:
        return None
    try:
        return uuid.UUID(str(user_id))
    except ValueError:
        return None


def vector_literal(vec: Sequence[float]) -> str:
    """pgvector text representation of a vector ('[0.1,0.2,...]')."""
    return "[" + ",".join(repr(float(v)) for v in vec) + "]"


def row_to_document(row: dict) -> Document:
    """A kb_chunks row as a LangChain Document with citation metadata."""
    meta = {k: row[k] for k in ("doc_id", "issuer", "title", "section", "page", "chunk", "source_file",
                                "source_type", "version")}
    meta["owner"] = str(row["owner"]) if row["owner"] is not None else None
    return Document(page_content=row["text"], metadata=meta, id=str(row["id"]))


def _embedding_dimensions(pool: ConnectionPool) -> int:
    """The declared size of kb_chunks.embedding (pgvector stores it as the column typmod)."""
    with pool.connection() as c:
        return c.execute("SELECT atttypmod AS dims FROM pg_attribute WHERE attrelid = 'kb_chunks'::regclass "
                         "AND attname = 'embedding'").fetchone()["dims"]


def _check_doc_ids(docs: list[Document], owner: uuid.UUID | None) -> None:
    """Owned chunks must use 'user-' doc ids and shared ones must not (no impersonation of corpus docs)."""
    for doc in docs:
        doc_id = doc.metadata.get("doc_id")
        if not doc_id:
            raise ValueError("every document needs a doc_id")
        if (owner is not None) != doc_id.startswith(USER_DOC_PREFIX):
            raise ValueError(f"doc_id {doc_id!r} does not match its owner scope "
                             f"(user documents use the {USER_DOC_PREFIX!r} prefix, shared ones must not)")


def _embed(docs: list[Document], embeddings: Embeddings | None, pool: ConnectionPool) -> list[str | None]:
    """Vector literals for the documents (computed before any DB write), or None per row without embeddings."""
    if embeddings is None:
        return [None] * len(docs)
    vectors = embeddings.embed_documents([d.page_content for d in docs])
    expected = _embedding_dimensions(pool)
    if any(len(v) != expected for v in vectors):
        raise ValueError(f"embedding dimension mismatch: kb_chunks.embedding has {expected} dimensions, "
                         f"the embeddings model returned {len(vectors[0])}; set EMBEDDING_DIMENSIONS={expected}")
    return [vector_literal(v) for v in vectors]


def add_chunks(docs: list[Document], owner: str | None, embeddings: Embeddings | None,
               pool: ConnectionPool) -> list[str]:
    """Index chunks for `owner` (None = shared); replace earlier rows of the same (owner, doc_id).

    Embeddings are computed before the transaction. Within it, an advisory lock per (owner, doc_id)
    serialises concurrent re-indexing, so a document is never duplicated. Returns the new row ids.
    """
    if not docs:
        return []
    owner_uuid = parse_user_id(owner)
    if owner is not None and owner_uuid is None:
        raise ValueError("owner must be a user id (UUID) or None for shared documents")
    _check_doc_ids(docs, owner_uuid)
    vectors = _embed(docs, embeddings, pool)
    doc_ids = sorted({d.metadata["doc_id"] for d in docs})
    ids: list[str] = []
    with pool.connection() as c, c.transaction():
        for doc_id in doc_ids:  # sorted: consistent lock order across sessions
            c.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (f"kb:{owner_uuid}:{doc_id}",))
        c.execute("DELETE FROM kb_chunks WHERE doc_id = ANY(%s) AND owner IS NOT DISTINCT FROM %s::uuid",
                  (doc_ids, owner_uuid))
        for position, (doc, vec) in enumerate(zip(docs, vectors)):
            m = doc.metadata
            row = c.execute(_INSERT, (
                m["doc_id"], owner_uuid, m.get("issuer") or "", m.get("title") or "", m.get("section"),
                m.get("page"), m.get("chunk", position), m.get("source_file") or "",
                m.get("source_type") or "reference", m.get("version"), doc.page_content, vec,
            )).fetchone()
            ids.append(str(row["id"]))
    return ids


def index_documents(docs: list[Document], owner: str | None, embeddings: Embeddings | None,
                    pool: ConnectionPool) -> int:
    """Index chunks (idempotent per owner + doc_id: re-indexing replaces); return the number of rows written.

    Without embeddings the rows are full-text-searchable only (embedding NULL).
    """
    return len(add_chunks(docs, owner, embeddings, pool))


class KbVectorStore(VectorStore):
    """LangChain VectorStore over kb_chunks, scoped to one user: reads see shared + own rows, writes are owned.

    `user_id=None` gives the shared scope (reads: shared rows only; writes: shared documents).
    """

    def __init__(self, pool: ConnectionPool, embeddings: Embeddings, user_id: str | None = None):
        """Bind to a pool, an embeddings model and a user scope."""
        self.pool, self._embeddings, self.user_id = pool, embeddings, user_id

    @property
    def embeddings(self) -> Embeddings:
        """The embeddings model used for queries and inserts."""
        return self._embeddings

    def add_documents(self, documents: list[Document], **kwargs: Any) -> list[str]:
        """Index documents in this store's scope (see add_chunks); return their row ids."""
        return add_chunks(list(documents), self.user_id, self._embeddings, self.pool)

    def add_texts(self, texts: Iterable[str], metadatas: list[dict] | None = None, **kwargs: Any) -> list[str]:
        """Index raw texts; doc ids default to a fresh id in this store's namespace."""
        texts = list(texts)
        metadatas = metadatas or [{} for _ in texts]
        prefix = USER_DOC_PREFIX if self.user_id is not None else "text-"
        docs = [Document(page_content=t, metadata={"doc_id": f"{prefix}{uuid.uuid4().hex[:16]}", **m})
                for t, m in zip(texts, metadatas)]
        return self.add_documents(docs)

    def similarity_search_by_vector_with_score(self, embedding: list[float], k: int = 4) -> list[tuple[Document, float]]:
        """Top-k visible chunks by cosine similarity (1 - distance); ties ordered by doc_id, chunk, id."""
        literal = vector_literal(embedding)
        with self.pool.connection() as c:
            rows = c.execute(_VECTOR_QUERY, (literal, parse_user_id(self.user_id), literal,
                                             k * VECTOR_CANDIDATE_FACTOR, k)).fetchall()
        return [(row_to_document(r), 1.0 - float(r["distance"])) for r in rows]

    def similarity_search_with_score(self, query: str, k: int = 4, **kwargs: Any) -> list[tuple[Document, float]]:
        """Embed the query and return the top-k visible chunks with cosine similarity."""
        return self.similarity_search_by_vector_with_score(self._embeddings.embed_query(query), k)

    def similarity_search(self, query: str, k: int = 4, **kwargs: Any) -> list[Document]:
        """Top-k visible chunks for a query."""
        return [doc for doc, _ in self.similarity_search_with_score(query, k)]

    def similarity_search_by_vector(self, embedding: list[float], k: int = 4, **kwargs: Any) -> list[Document]:
        """Top-k visible chunks for a query vector."""
        return [doc for doc, _ in self.similarity_search_by_vector_with_score(embedding, k)]

    def _select_relevance_score_fn(self):
        """Scores are already cosine similarities; map [-1, 1] to [0, 1] for relevance-score APIs."""
        return lambda score: (score + 1.0) / 2.0

    def delete(self, ids: list[str] | None = None, **kwargs: Any) -> bool | None:
        """Delete rows by id within this store's write scope (own rows, or shared rows for the shared scope)."""
        if not ids:
            return False
        valid = [u for u in (parse_user_id(i) for i in ids) if u is not None]
        with self.pool.connection() as c:
            c.execute("DELETE FROM kb_chunks WHERE id = ANY(%s) AND owner IS NOT DISTINCT FROM %s::uuid",
                      (valid, parse_user_id(self.user_id)))
        return True

    @classmethod
    def from_texts(cls, texts: list[str], embedding: Embeddings, metadatas: list[dict] | None = None, *,
                   pool: ConnectionPool | None = None, user_id: str | None = None,
                   **kwargs: Any) -> "KbVectorStore":
        """Create a store for `user_id` on `pool` and index the texts into it."""
        if pool is None:
            raise ValueError("KbVectorStore.from_texts needs pool=")
        store = cls(pool, embedding, user_id=user_id)
        store.add_texts(texts, metadatas)
        return store
