-- RAG knowledge base (M5): chunks of shared reference documents (owner NULL) and of user-uploaded
-- reference documents (owner = the uploading user, visible only to them). 003 is reserved for M3.
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS kb_chunks (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    doc_id      text NOT NULL,
    owner       uuid NULL REFERENCES users(id) ON DELETE CASCADE,
    issuer      text NOT NULL,
    title       text NOT NULL,
    section     text NULL,
    page        integer NULL,
    chunk       integer NOT NULL,
    source_file text NOT NULL,
    source_type text NOT NULL DEFAULT 'reference',
    version     text NULL,
    text        text NOT NULL,
    -- full-text fallback (D-011): body weighted above section, section above title
    tsv         tsvector GENERATED ALWAYS AS (
                    setweight(to_tsvector('english', coalesce(text, '')), 'A') ||
                    setweight(to_tsvector('english', coalesce(section, '')), 'B') ||
                    setweight(to_tsvector('english', coalesce(title, '')), 'C')
                ) STORED,
    -- NULL when indexed without an embeddings provider (FTS-only rows); size = EMBEDDING_DIMENSIONS
    embedding   vector(1536) NULL,
    created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS kb_chunks_tsv ON kb_chunks USING gin (tsv);
CREATE INDEX IF NOT EXISTS kb_chunks_embedding_hnsw ON kb_chunks USING hnsw (embedding vector_cosine_ops);
CREATE INDEX IF NOT EXISTS kb_chunks_owner_doc ON kb_chunks (owner, doc_id);
