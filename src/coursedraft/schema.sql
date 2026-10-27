CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS courses (
    id               TEXT PRIMARY KEY,
    title            TEXT NOT NULL,
    duration_minutes INT  NOT NULL,
    objectives       JSONB NOT NULL
);

-- One row per ingested file version. A file whose hash changes gets a new version, and the
-- old version's chunks stay so an approved draft can always be traced to what it was built on.
CREATE TABLE IF NOT EXISTS sources (
    id           SERIAL PRIMARY KEY,
    course_id    TEXT NOT NULL REFERENCES courses(id),
    path         TEXT NOT NULL,
    source_type  TEXT NOT NULL CHECK (source_type IN
                    ('own_deck', 'publisher_deck', 'standard', 'client_notes')),
    sensitivity  TEXT NOT NULL CHECK (sensitivity IN ('public', 'internal', 'restricted')),
    sha256       TEXT NOT NULL,
    version      INT  NOT NULL,
    current      BOOLEAN NOT NULL DEFAULT TRUE,
    ingested_at  TIMESTAMPTZ NOT NULL,
    UNIQUE (course_id, path, version)
);

CREATE TABLE IF NOT EXISTS chunks (
    id              TEXT PRIMARY KEY,
    source_id       INT  NOT NULL REFERENCES sources(id),
    course_id       TEXT NOT NULL REFERENCES courses(id),
    kind            TEXT NOT NULL CHECK (kind IN ('slide', 'notes', 'section')),
    locator         TEXT NOT NULL,
    title           TEXT NOT NULL DEFAULT '',
    body            TEXT NOT NULL,
    audience        TEXT NOT NULL CHECK (audience IN ('participant', 'facilitator')),
    objective_ids   TEXT[] NOT NULL DEFAULT '{}',
    paragraph_refs  TEXT[] NOT NULL DEFAULT '{}',
    tsv             tsvector GENERATED ALWAYS AS
                        (to_tsvector('english', title || ' ' || body)) STORED,
    embedding       vector({embed_dim}) NOT NULL
);
CREATE INDEX IF NOT EXISTS chunks_tsv_idx ON chunks USING gin (tsv);
CREATE INDEX IF NOT EXISTS chunks_embedding_idx ON chunks USING hnsw (embedding vector_cosine_ops);
CREATE INDEX IF NOT EXISTS chunks_refs_idx ON chunks USING gin (paragraph_refs);

CREATE TABLE IF NOT EXISTS fact_records (
    course_id  TEXT PRIMARY KEY REFERENCES courses(id),
    record     JSONB NOT NULL
);

CREATE TABLE IF NOT EXISTS runs (
    id          TEXT PRIMARY KEY,
    course_id   TEXT NOT NULL REFERENCES courses(id),
    kind        TEXT NOT NULL DEFAULT 'course' CHECK (kind IN ('course', 'eval')),
    status      TEXT NOT NULL CHECK (status IN
                    ('drafting', 'awaiting_approval', 'returned', 'exported', 'failed')),
    created_at  TIMESTAMPTZ NOT NULL,
    updated_at  TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS draft_versions (
    id          SERIAL PRIMARY KEY,
    run_id      TEXT NOT NULL REFERENCES runs(id),
    version     INT  NOT NULL,
    draft       JSONB NOT NULL,
    validation  JSONB NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL,
    UNIQUE (run_id, version)
);

CREATE TABLE IF NOT EXISTS approvals (
    id                SERIAL PRIMARY KEY,
    draft_version_id  INT  NOT NULL UNIQUE REFERENCES draft_versions(id),
    decision          TEXT NOT NULL CHECK (decision IN ('approved', 'returned')),
    reviewer          TEXT NOT NULL,
    notes             TEXT NOT NULL DEFAULT '',
    source_chunk_ids  TEXT[] NOT NULL,
    decided_at        TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS exports (
    id           SERIAL PRIMARY KEY,
    approval_id  INT  NOT NULL REFERENCES approvals(id),
    kind         TEXT NOT NULL CHECK (kind IN ('deck', 'guide', 'case')),
    path         TEXT NOT NULL,
    sha256       TEXT NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL
);

-- Every model call, which route it took and exactly which chunks were sent.
CREATE TABLE IF NOT EXISTS model_calls (
    id          SERIAL PRIMARY KEY,
    run_id      TEXT NOT NULL REFERENCES runs(id),
    step        TEXT NOT NULL,
    route       TEXT NOT NULL CHECK (route IN ('hosted', 'local')),
    adapter     TEXT NOT NULL,
    model       TEXT NOT NULL,
    sensitivity TEXT NOT NULL,
    chunk_ids   TEXT[] NOT NULL,
    chars_sent  INT  NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL
);
