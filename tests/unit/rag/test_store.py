"""kb_chunks store + user-scoped retrieval: vector search, full-text fallback, idempotent indexing (Task 3)."""
from pathlib import Path

import pytest
from langchain_core.documents import Document
from langchain_core.embeddings import DeterministicFakeEmbedding
from langchain_core.vectorstores import VectorStore

from statbrief.auth.users import create_user
from statbrief.rag.corpus import index_corpus, load_corpus
from statbrief.rag.embeddings import make_embeddings
from statbrief.rag.loaders import USER_ISSUER, load_user_reference, split_documents
from statbrief.rag.retriever import RetrievedPassage, retrieve
from statbrief.rag.store import _VECTOR_QUERY, KbVectorStore, index_documents, vector_literal
from statbrief.storage.db import bootstrap, create_pool
from tests.unit.rag.embeddings import KeywordHashEmbeddings

KB = Path(__file__).resolve().parents[3] / "knowledge_base"
SECRET_NOTE = (b"# Internal note\n\n## 1 Zymurgy method\nOur zymurgy fermentation index uses a "
               b"proprietary deflator for regional brewing output.\n")


@pytest.fixture
def ctx(settings):
    """Bootstrapped DB, pool, two users (alice, bob) and offline embeddings."""
    bootstrap(settings)
    pool = create_pool(settings)
    alice = create_user(pool, "alice@example.com", "long-enough-pw", 10, 128)
    bob = create_user(pool, "bob@example.com", "long-enough-pw", 10, 128)
    yield pool, alice, bob, KeywordHashEmbeddings(settings.embedding_dimensions)
    pool.close()


@pytest.fixture
def indexed(ctx, settings):
    """ctx with the shared knowledge base indexed (embeddings included)."""
    pool, _, _, emb = ctx
    assert index_corpus(pool, settings, emb, KB) > 0
    return ctx


def _upload(pool, owner, emb, settings, data=SECRET_NOTE, name="note.md") -> int:
    """Load, split and index one user-uploaded reference document."""
    docs = load_user_reference(data, name, owner=owner, max_bytes=10_000, max_pages=10)
    return index_documents(split_documents(docs, settings.rag_chunk_size, settings.rag_chunk_overlap), owner, emb, pool)


def _count(pool, where: str = "TRUE", params=()) -> int:
    """Number of kb_chunks rows matching a condition."""
    with pool.connection() as c:
        return c.execute(f"SELECT count(*) AS n FROM kb_chunks WHERE {where}", params).fetchone()["n"]


def test_migration_creates_table_vector_and_fts_indexes(ctx, settings):
    """kb_chunks has a vector(<dims>) column, a GIN index on tsv and an HNSW cosine index."""
    pool = ctx[0]
    with pool.connection() as c:
        dims = c.execute("SELECT atttypmod AS d FROM pg_attribute WHERE attrelid = 'kb_chunks'::regclass "
                         "AND attname = 'embedding'").fetchone()["d"]
        indexes = {r["indexdef"] for r in c.execute("SELECT indexdef FROM pg_indexes WHERE tablename = 'kb_chunks'")}
    assert dims == settings.embedding_dimensions
    assert any("USING gin (tsv)" in i for i in indexes)
    assert any("USING hnsw (embedding vector_cosine_ops)" in i for i in indexes)


def test_vector_retrieval_finds_hicp_regulation(indexed, settings):
    """'HICP base year' retrieves the HICP regulation's base-year article by vector similarity."""
    pool, alice, _, emb = indexed
    hits = retrieve(pool, "HICP base year", alice.id, settings, embeddings=emb)
    assert hits and all(isinstance(h, RetrievedPassage) and h.method == "vector" for h in hits)
    assert len(hits) == settings.rag_top_k
    assert hits[0].doc_id == "ESMB-REG-2023-14"
    assert hits[0].issuer == "ESMB" and hits[0].page is not None and hits[0].section
    assert hits == sorted(hits, key=lambda h: -h.score)


def test_fts_fallback_when_vector_search_disabled(indexed, settings):
    """With ENABLE_VECTOR_SEARCH=false, 'NUTS 3 Cluj' is answered by full-text search from the NUTS doc."""
    pool, alice, _, emb = indexed
    off = settings.model_copy(update={"enable_vector_search": False})
    hits = retrieve(pool, "NUTS 3 Cluj", alice.id, off, embeddings=emb)
    assert hits and {h.method for h in hits} == {"fts"}
    assert hits[0].doc_id == "ESMB-REG-NUTS" and "Cluj" in hits[0].text
    assert hits[0].section and hits[0].title.startswith("ESMB Regulation on the Territorial")


def test_fts_fallback_without_embeddings(indexed, settings):
    """No embeddings (no API key) means full-text search even when vector search is enabled."""
    pool, alice, _, _ = indexed
    hits = retrieve(pool, "NUTS 3 Cluj", alice.id, settings, embeddings=None)
    assert hits[0].method == "fts" and hits[0].doc_id == "ESMB-REG-NUTS"


def test_fts_relaxes_to_any_term_when_all_terms_match_nothing(indexed, settings):
    """A query with an unknown word still finds passages containing the other words."""
    pool, alice, _, _ = indexed
    hits = retrieve(pool, "Cluj qwertyuiopword", alice.id, settings, embeddings=None)
    assert hits and all("Cluj" in h.text for h in hits)
    assert "ESMB-REG-NUTS" in {h.doc_id for h in hits}


def test_rows_without_embeddings_are_fts_only(ctx, settings):
    """Indexing without embeddings stores FTS rows; vector search then falls back to FTS."""
    pool, alice, _, emb = ctx
    assert index_corpus(pool, settings, None, KB) > 0
    assert _count(pool, "embedding IS NOT NULL") == 0
    hits = retrieve(pool, "NUTS 3 Cluj", alice.id, settings, embeddings=emb)
    assert hits and hits[0].method == "fts"


def test_user_documents_are_private_to_their_owner(indexed, settings):
    """Bob can retrieve neither by vector nor by full text the document Alice uploaded."""
    pool, alice, bob, emb = indexed
    assert _upload(pool, alice.id, emb, settings) > 0
    for embeddings in (emb, None):
        mine = retrieve(pool, "zymurgy fermentation index", alice.id, settings, embeddings=embeddings)
        assert mine[0].doc_id.startswith("user-") and "zymurgy" in mine[0].text
        theirs = retrieve(pool, "zymurgy fermentation index", bob.id, settings, embeddings=embeddings)
        assert all(not h.doc_id.startswith("user-") for h in theirs)
    assert retrieve(pool, "NUTS 3 Cluj", bob.id, settings)[0].doc_id == "ESMB-REG-NUTS"  # shared stays visible
    for bad in ("not-a-uuid", None):
        assert all(not h.doc_id.startswith("user-")
                   for h in retrieve(pool, "zymurgy", bad, settings, embeddings=emb))


def test_reindex_replaces_rows_and_is_scoped_to_owner(indexed, settings):
    """Re-indexing a document replaces its rows; the same upload by two users is kept twice."""
    pool, alice, bob, emb = indexed
    shared = _count(pool)
    assert index_corpus(pool, settings, emb, KB) == shared
    assert _count(pool) == shared
    n = _upload(pool, alice.id, emb, settings)
    _upload(pool, bob.id, emb, settings)
    _upload(pool, bob.id, emb, settings)
    assert _count(pool, "owner = %s", (alice.id,)) == n
    assert _count(pool, "owner = %s", (bob.id,)) == n
    assert _count(pool, "owner IS NULL") == shared


def test_deleting_a_user_deletes_their_chunks(indexed, settings):
    """kb_chunks.owner cascades on user deletion."""
    pool, alice, _, emb = indexed
    _upload(pool, alice.id, emb, settings)
    with pool.connection() as c:
        c.execute("DELETE FROM users WHERE id = %s", (alice.id,))
    assert _count(pool, "owner IS NOT NULL") == 0


def test_doc_id_namespaces_are_enforced(ctx):
    """Owned rows must use 'user-' doc ids and shared rows must not (no impersonation of corpus docs)."""
    pool, alice, _, emb = ctx
    meta = {"title": "T", "issuer": "ESMB", "section": None, "page": None, "source_file": "f.md"}
    with pytest.raises(ValueError):
        index_documents([Document("x", metadata={**meta, "doc_id": "ESMB-REG-NUTS"})], alice.id, emb, pool)
    with pytest.raises(ValueError):
        index_documents([Document("x", metadata={**meta, "doc_id": "user-abc"})], None, emb, pool)
    with pytest.raises(ValueError):
        index_documents([Document("x", metadata={**meta, "doc_id": "user-abc"})], "not-a-uuid", emb, pool)


def test_embedding_dimension_mismatch_is_rejected(ctx):
    """Vectors whose size differs from the column are refused before any write."""
    pool, alice, _, _ = ctx
    docs = [Document("x", metadata={"doc_id": "ESMB-T", "title": "T", "issuer": "ESMB", "source_file": "t.md"})]
    with pytest.raises(ValueError, match="dimension"):
        index_documents(docs, None, DeterministicFakeEmbedding(size=8), pool)
    assert _count(pool) == 0


def test_ties_are_ordered_deterministically(ctx, settings):
    """Equal scores are ordered by doc_id then chunk, identically on every call."""
    pool, alice, _, emb = ctx
    docs = [Document("identical tie text about quarterly volumes",
                     metadata={"doc_id": d, "title": "T", "issuer": "ESMB", "source_file": "t.txt", "chunk": 0})
            for d in ("ESMB-B", "ESMB-A", "ESMB-C")]
    index_documents(docs, None, emb, pool)
    for embeddings in (emb, None):
        first = retrieve(pool, "quarterly volumes", alice.id, settings, embeddings=embeddings)
        assert [h.doc_id for h in first] == ["ESMB-A", "ESMB-B", "ESMB-C"]
        assert retrieve(pool, "quarterly volumes", alice.id, settings, embeddings=embeddings) == first


def test_blank_query_returns_nothing(indexed, settings):
    """Blank or stop-word-only queries return no passages."""
    pool, alice, _, emb = indexed
    assert retrieve(pool, "   ", alice.id, settings, embeddings=emb) == []
    assert retrieve(pool, "the", alice.id, settings, embeddings=None) == []


def test_passage_citation_and_metadata(indexed, settings):
    """Passages carry everything needed for a citation: document, issuer, section and page."""
    pool, alice, _, emb = indexed
    hit = retrieve(pool, "HICP base year", alice.id, settings, embeddings=emb)[0]
    assert hit.citation() == f"{hit.title} ({hit.issuer}), {hit.section}, p. {hit.page}"
    md = retrieve(pool, "NUTS 3 Cluj", alice.id, settings, embeddings=None)[0]
    assert md.page is None and md.citation() == f"{md.title} ({md.issuer}), {md.section}"


def test_kb_vector_store_is_a_scoped_langchain_vector_store(indexed, settings):
    """KbVectorStore works through the LangChain VectorStore / retriever interfaces, scoped to its user."""
    pool, alice, bob, emb = indexed
    _upload(pool, alice.id, emb, settings)
    store = KbVectorStore(pool, emb, user_id=bob.id)
    assert isinstance(store, VectorStore) and store.embeddings is emb
    docs = store.as_retriever(search_kwargs={"k": 3}).invoke("zymurgy fermentation")
    assert len(docs) == 3 and all(not d.metadata["doc_id"].startswith("user-") for d in docs)
    scored = KbVectorStore(pool, emb, user_id=alice.id).similarity_search_with_score("zymurgy fermentation", k=1)
    assert scored[0][0].metadata["doc_id"].startswith("user-") and 0 < scored[0][1] <= 1
    ids = KbVectorStore(pool, emb, user_id=bob.id).add_texts(["bob's zymurgy notes"], [{"source_file": "b.txt"}])
    assert len(ids) == 1 and _count(pool, "owner = %s", (bob.id,)) == 1


def test_make_embeddings_needs_key_and_toggle(settings):
    """No key or vector search disabled -> None; with a key -> OpenAI embeddings configured from settings."""
    assert make_embeddings(settings) is None
    keyed = settings.model_copy(update={"openai_api_key": "sk-test"})
    emb = make_embeddings(keyed)
    assert emb.model == settings.embeddings_model and emb.dimensions == settings.embedding_dimensions
    assert make_embeddings(keyed.model_copy(update={"enable_vector_search": False})) is None


def test_load_corpus_uses_manifest_metadata():
    """Corpus documents carry manifest ids, titles, issuers and versions; none is owned."""
    docs = load_corpus(KB)
    assert {d.metadata["doc_id"] for d in docs} == {
        "ESMB-REG-2023-14", "ESMB-MAN-LFS", "ESMB-GUIDE-NA-VOL", "ESMB-REG-NUTS", "ESMB-POL-QUALITY",
        "IRS-DD-REG-MONTHLY", "IRS-TN-EUR-DIFF", "IRS-CAL-DISSEM", "ESMB-GUIDE-COMMENTARY"}
    assert all(d.metadata["owner"] is None and d.metadata["version"] for d in docs)
    assert {d.metadata["source_type"] for d in docs} == {"reference"}


def test_index_script_runs_without_api_key(db_url, monkeypatch, capsys):
    """scripts/index_knowledge_base.py bootstraps the DB and writes FTS-only rows when no key exists."""
    import importlib.util

    from statbrief.config import get_settings

    monkeypatch.setenv("DATABASE_URL", db_url)
    monkeypatch.setenv("SESSION_SECRET", "s" * 32)
    monkeypatch.setenv("APP_ENV", "test")
    get_settings.cache_clear()
    try:
        spec = importlib.util.spec_from_file_location("index_kb", KB.parent / "scripts" / "index_knowledge_base.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        written = module.main([])
        assert written > 0 and module.main(["--fts-only"]) == written
    finally:
        get_settings.cache_clear()
    assert "full-text search only" in capsys.readouterr().out


class _FailingEmbeddings(KeywordHashEmbeddings):
    """Embeddings whose provider is down (e.g. network error or revoked key)."""

    def embed_query(self, text: str) -> list[float]:
        """Always fail."""
        raise RuntimeError("provider unavailable sk-secret")


def test_embeddings_failure_falls_back_to_fts(indexed, settings, caplog):
    """A failing embeddings call degrades to full-text search, logged without query text or key."""
    pool, alice, _, _ = indexed
    with caplog.at_level("WARNING", logger="statbrief.rag.retriever"):
        hits = retrieve(pool, "NUTS 3 Cluj", alice.id, settings, embeddings=_FailingEmbeddings())
    assert hits and {h.method for h in hits} == {"fts_fallback"} and hits[0].doc_id == "ESMB-REG-NUTS"
    assert "RuntimeError" in caplog.text
    assert "Cluj" not in caplog.text and "sk-secret" not in caplog.text


def test_filtered_hnsw_scan_still_returns_k_owner_rows(ctx, settings):
    """Hundreds of other-user rows nearer the query than the visible rows do not starve the owner."""
    import psycopg

    pool, alice, bob, emb = ctx
    query = "regional brewing deflator"
    near = [Document(f"{query} b{i:04d}", metadata={"doc_id": f"user-b{i:04d}", "title": "B", "issuer": USER_ISSUER,
                                      "source_file": "b.txt"}) for i in range(300)]
    index_documents(near, bob.id, emb, pool)
    mine = [Document(f"{query} note number {i} with extra words", metadata={
        "doc_id": f"user-a{i}", "title": "A", "issuer": USER_ISSUER, "source_file": "a.txt"}) for i in range(8)]
    for doc in mine:
        index_documents([doc], alice.id, emb, pool)
    with pool.connection() as c:  # make the planner use the HNSW index for every new connection
        db = c.execute("SELECT current_database() AS d").fetchone()["d"]
        c.execute(f'ALTER DATABASE "{db}" SET enable_seqscan = off')
        c.execute(f'ALTER DATABASE "{db}" SET enable_bitmapscan = off')
    vec = vector_literal(emb.embed_query(query))
    with psycopg.connect(settings.database_url) as c:
        plan = "\n".join(r[0] for r in c.execute("EXPLAIN " + _VECTOR_QUERY, (vec, alice.id, vec, 24, 6)))
    assert "kb_chunks_embedding_hnsw" in plan, plan  # the filtered query really scans the HNSW index
    fresh = create_pool(settings)
    try:
        hits = retrieve(fresh, query, alice.id, settings, embeddings=emb, k=6)
    finally:
        fresh.close()
    assert len(hits) == 6 and all(h.doc_id.startswith("user-a") and h.method == "vector" for h in hits)


def test_vector_mode_also_finds_unembedded_own_rows(indexed, settings):
    """Rows stored without embeddings (e.g. indexed while the provider was down) stay findable in vector mode."""
    pool, alice, _, emb = indexed
    _upload(pool, alice.id, None, settings)
    hits = retrieve(pool, "zymurgy fermentation index", alice.id, settings, embeddings=emb)
    assert hits[0].method == "vector"
    assert any(h.doc_id.startswith("user-") and h.method == "fts" for h in hits)
    assert len(hits) == settings.rag_top_k


def test_fts_requires_a_positive_term(indexed, settings):
    """Negation-only queries would match almost everything with rank 0; they return nothing."""
    pool, alice, _, _ = indexed
    assert retrieve(pool, "-inflation", alice.id, settings, embeddings=None) == []
    assert retrieve(pool, "-inflation -unemployment", alice.id, settings, embeddings=None) == []
    assert all(h.score > 0 for h in retrieve(pool, "Cluj -NUTS", alice.id, settings, embeddings=None))


def test_k_is_clamped_to_rag_max_k(indexed, settings):
    """A caller-supplied k never exceeds RAG_MAX_K."""
    pool, alice, _, emb = indexed
    capped = settings.model_copy(update={"rag_max_k": 3})
    assert len(retrieve(pool, "HICP base year", alice.id, capped, embeddings=emb, k=1000)) == 3
    assert len(retrieve(pool, "NUTS region", alice.id, capped, embeddings=None, k=1000)) == 3
    assert settings.rag_max_k == 50


def test_kb_vector_store_from_texts_and_scoped_delete(ctx):
    """from_texts indexes into the given scope; delete() only removes rows the scope owns."""
    pool, alice, bob, emb = ctx
    store = KbVectorStore.from_texts(["alice zymurgy text"], emb, [{"source_file": "a.txt"}], pool=pool,
                                     user_id=alice.id)
    (doc,) = store.similarity_search("zymurgy", k=1)
    assert doc.metadata["owner"] == alice.id
    KbVectorStore(pool, emb, user_id=bob.id).delete([doc.id])
    assert _count(pool, "owner = %s", (alice.id,)) == 1
    KbVectorStore(pool, emb, user_id=None).delete([doc.id])  # the shared scope cannot delete owned rows
    assert _count(pool, "owner = %s", (alice.id,)) == 1
    store.delete([doc.id, "not-a-uuid"])
    assert _count(pool, "owner = %s", (alice.id,)) == 0
    with pytest.raises(ValueError):
        KbVectorStore.from_texts(["x"], emb)


def test_openai_embeddings_skip_tiktoken_length_check(settings):
    """check_embedding_ctx_length=False: no tiktoken encoding download on a cold start."""
    emb = make_embeddings(settings.model_copy(update={"openai_api_key": "sk-test"}))
    assert emb.check_embedding_ctx_length is False
