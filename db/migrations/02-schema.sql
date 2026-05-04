-- Core RAG schema. Idempotent so it can run on init or be re-applied.

CREATE TABLE IF NOT EXISTS documents (
    id                   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    original_filename    TEXT        NOT NULL,
    file_type            TEXT        NOT NULL,
    storage_path         TEXT        NOT NULL,
    markdown_storage_path TEXT,
    markdown_text        TEXT,
    status               TEXT        NOT NULL DEFAULT 'uploaded'
        CHECK (status IN ('uploaded','processing','completed','failed')),
    error_message        TEXT,
    user_id              TEXT,
    collection           TEXT,
    metadata             JSONB       NOT NULL DEFAULT '{}'::jsonb,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at           TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS documents_status_idx     ON documents (status);
CREATE INDEX IF NOT EXISTS documents_collection_idx ON documents (collection);
CREATE INDEX IF NOT EXISTS documents_user_idx       ON documents (user_id);

CREATE TABLE IF NOT EXISTS document_chunks (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    document_id  UUID NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    chunk_index  INTEGER NOT NULL,
    page_number  INTEGER,
    heading      TEXT,
    content      TEXT NOT NULL,
    token_count  INTEGER,
    -- Embedding dimension is provider-specific. text-embedding-3-small = 1536.
    -- If you switch models, run a migration to alter this column + reprocess.
    embedding    vector(1536),
    metadata     JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (document_id, chunk_index)
);

CREATE INDEX IF NOT EXISTS chunks_document_idx ON document_chunks (document_id);
CREATE INDEX IF NOT EXISTS chunks_metadata_idx ON document_chunks USING GIN (metadata);

-- HNSW for cosine similarity. Requires pgvector >= 0.5 (pg16 image ships it).
CREATE INDEX IF NOT EXISTS chunks_embedding_hnsw
    ON document_chunks USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 64);

CREATE TABLE IF NOT EXISTS ingestion_jobs (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    document_id   UUID NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    status        TEXT NOT NULL DEFAULT 'queued'
        CHECK (status IN ('queued','running','completed','failed')),
    started_at    TIMESTAMPTZ,
    completed_at  TIMESTAMPTZ,
    error_message TEXT,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS jobs_document_idx ON ingestion_jobs (document_id);
CREATE INDEX IF NOT EXISTS jobs_status_idx   ON ingestion_jobs (status);

CREATE OR REPLACE FUNCTION set_updated_at() RETURNS TRIGGER AS $$
BEGIN NEW.updated_at = now(); RETURN NEW; END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS documents_updated_at ON documents;
CREATE TRIGGER documents_updated_at
    BEFORE UPDATE ON documents
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();
