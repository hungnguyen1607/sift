CREATE EXTENSION IF NOT EXISTS vector;

-- One row per fetched page. Full-text search over title (weight A) + body (B).
CREATE TABLE pages (
    id          bigserial PRIMARY KEY,
    url         text NOT NULL UNIQUE,
    title       text,
    content     text NOT NULL DEFAULT '',
    source      text NOT NULL,
    published   timestamptz,
    updated     timestamptz,
    fetched_at  timestamptz NOT NULL DEFAULT now(),
    tsv tsvector GENERATED ALWAYS AS (
        setweight(to_tsvector('english', coalesce(title, '')), 'A') ||
        setweight(to_tsvector('english', content), 'B')
    ) STORED
);
CREATE INDEX pages_tsv_idx ON pages USING gin (tsv);

-- ~300-word passages with their character offsets in pages.content, so a
-- citation can point at the exact quote. Embeddings: bge-small (384 dims),
-- stored as halfvec to halve disk and RAM.
CREATE TABLE passages (
    id          bigserial PRIMARY KEY,
    page_id     bigint NOT NULL REFERENCES pages(id) ON DELETE CASCADE,
    ord         int NOT NULL,
    content     text NOT NULL,
    char_start  int NOT NULL,
    char_end    int NOT NULL,
    embedding   halfvec(384),
    tsv tsvector GENERATED ALWAYS AS (to_tsvector('english', content)) STORED,
    UNIQUE (page_id, ord),
    CHECK (char_end >= char_start)
);
CREATE INDEX passages_tsv_idx ON passages USING gin (tsv);
CREATE INDEX passages_embedding_idx ON passages USING hnsw (embedding halfvec_cosine_ops);

-- Background job queue, consumed with SELECT ... FOR UPDATE SKIP LOCKED.
CREATE TABLE jobs (
    id          bigserial PRIMARY KEY,
    kind        text NOT NULL,
    payload     jsonb NOT NULL DEFAULT '{}',
    status      text NOT NULL DEFAULT 'queued'
                CHECK (status IN ('queued', 'running', 'done', 'failed')),
    attempts    int NOT NULL DEFAULT 0,
    last_error  text,
    run_after   timestamptz NOT NULL DEFAULT now(),
    created_at  timestamptz NOT NULL DEFAULT now(),
    updated_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX jobs_ready_idx ON jobs (run_after) WHERE status = 'queued';
