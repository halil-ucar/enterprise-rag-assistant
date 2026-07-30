-- Schema: enterprise geometry from day 1 (tenant, lineage, ACL, data class, embedding version).
-- Runs once on first container start (docker-entrypoint-initdb.d), owner: rag_owner.

CREATE EXTENSION IF NOT EXISTS vector;

-- ── documents: deletion lineage root ─────────────────────────────────────────
CREATE TABLE documents (
    doc_id        text PRIMARY KEY,
    tenant        text NOT NULL,
    collection    text NOT NULL,
    title         text NOT NULL,
    department    text NOT NULL,          -- 'all' = visible to every department
    content_hash  text NOT NULL,          -- idempotent ingestion (doc_id + hash)
    created_at    timestamptz NOT NULL DEFAULT now(),
    updated_at    timestamptz NOT NULL DEFAULT now()
);

-- ── chunks: the retrieval unit ───────────────────────────────────────────────
-- department/tenant are denormalized so RLS filters without a join.
CREATE TABLE chunks (
    id                bigserial PRIMARY KEY,
    doc_id            text NOT NULL REFERENCES documents(doc_id) ON DELETE CASCADE,
    tenant            text NOT NULL,
    collection        text NOT NULL,
    department        text NOT NULL,
    seq               int  NOT NULL,
    section_path      text NOT NULL,
    page              int,
    is_table          boolean NOT NULL DEFAULT false,
    token_estimate    int NOT NULL DEFAULT 0,
    content           text NOT NULL,
    embedding_version int NOT NULL DEFAULT 1,
    -- BGE-M3 dimensionality; integration tests use a 1024-dim fake embedder.
    embedding         vector(1024) NOT NULL,
    -- Dual FTS: 'german' stems inflections, 'simple' preserves error codes/product names.
    tsv_german        tsvector GENERATED ALWAYS AS (to_tsvector('german', content)) STORED,
    tsv_simple        tsvector GENERATED ALWAYS AS (to_tsvector('simple', content)) STORED
);

-- HNSW: sublinear ANN. m/ef_construction = pgvector defaults, documented dials.
CREATE INDEX chunks_embedding_hnsw ON chunks
    USING hnsw (embedding vector_cosine_ops) WITH (m = 16, ef_construction = 64);
CREATE INDEX chunks_tsv_german_gin ON chunks USING gin (tsv_german);
CREATE INDEX chunks_tsv_simple_gin ON chunks USING gin (tsv_simple);
CREATE INDEX chunks_doc_id ON chunks (doc_id);
CREATE INDEX chunks_collection ON chunks (tenant, collection, embedding_version);

-- ── collection state: corpus_version invalidates the answer cache implicitly ─
CREATE TABLE collection_state (
    tenant         text NOT NULL,
    collection     text NOT NULL,
    corpus_version int  NOT NULL DEFAULT 0,
    PRIMARY KEY (tenant, collection)
);

-- ── feedback: online quality signal (ad-line "continuous optimization") ──────
CREATE TABLE feedback (
    id                 bigserial PRIMARY KEY,
    tenant             text NOT NULL,
    collection         text NOT NULL,
    department         text NOT NULL,
    user_id            text NOT NULL,
    rating             int  NOT NULL CHECK (rating IN (-1, 1)),
    condensed_question text NOT NULL,
    route              text NOT NULL DEFAULT 'direct',
    cited_doc_ids      text[] NOT NULL DEFAULT '{}',   -- deletion-cascade hook
    created_at         timestamptz NOT NULL DEFAULT now()
);
