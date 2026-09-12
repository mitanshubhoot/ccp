-- Phase 7b: the retrieval index (spec.md A-43, A-46) and the content-hash caches.
--
-- chunks is a derived index over current evidence, rebuilt by `ccp index rebuild`; it is the
-- one tenant table the app role may UPDATE and DELETE, because nothing audits it and a
-- chunk id is a content hash (tenant, evidence, chunker version, page, ordinal, text), so a
-- re-index of unchanged text is a no-op and a verdict's retrieved_chunk_ids pin exact text.
-- Distractor chunks (is_distractor) exist for retrieval evaluation and percentile
-- calibration only; evaluators never retrieve them.

CREATE TABLE IF NOT EXISTS chunks (
  chunk_id            TEXT PRIMARY KEY,
  tenant_id           TEXT NOT NULL,
  evidence_entity_id  TEXT NOT NULL,
  evidence_type       TEXT NOT NULL,
  subject_entity_id   TEXT,
  source_ref          TEXT NOT NULL,
  page                INT,                           -- 1-based PDF page; NULL for plain text
  ordinal             INT NOT NULL,                  -- chunk order within the evidence row
  text                TEXT NOT NULL,
  context             TEXT,                          -- contextual enrichment, when enabled
  tsv                 tsvector GENERATED ALWAYS AS
                        (to_tsvector('english', coalesce(context, '') || ' ' || text)) STORED,
  embedding           vector(1024),
  embed_model         TEXT NOT NULL,
  chunker_version     TEXT NOT NULL,
  text_sha256         TEXT NOT NULL,
  is_distractor       BOOLEAN NOT NULL DEFAULT false,
  indexed_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_chunks_tsv ON chunks USING gin (tsv);
CREATE INDEX IF NOT EXISTS ix_chunks_embedding ON chunks USING hnsw (embedding vector_cosine_ops);
CREATE INDEX IF NOT EXISTS ix_chunks_scope ON chunks (tenant_id, evidence_type, subject_entity_id);
CREATE INDEX IF NOT EXISTS ix_chunks_evidence ON chunks (tenant_id, evidence_entity_id);

CREATE TABLE IF NOT EXISTS embedding_cache (            -- sha256(model | input_type | text)
  cache_key   TEXT PRIMARY KEY,
  tenant_id   TEXT NOT NULL,
  embed_model TEXT NOT NULL,
  embedding   vector(1024) NOT NULL,
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS enrichment_cache (           -- sha256(model | prompt_version | doc | chunk)
  cache_key      TEXT PRIMARY KEY,
  tenant_id      TEXT NOT NULL,
  model          TEXT NOT NULL,
  prompt_version TEXT NOT NULL,
  context        TEXT NOT NULL,
  created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

GRANT SELECT, INSERT, UPDATE, DELETE ON chunks TO {{app_role}};
GRANT SELECT, INSERT ON embedding_cache, enrichment_cache TO {{app_role}};

ALTER TABLE chunks ENABLE ROW LEVEL SECURITY;
ALTER TABLE chunks FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON chunks
  USING (tenant_id = app_tenant()) WITH CHECK (tenant_id = app_tenant());

ALTER TABLE embedding_cache ENABLE ROW LEVEL SECURITY;
ALTER TABLE embedding_cache FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON embedding_cache
  USING (tenant_id = app_tenant()) WITH CHECK (tenant_id = app_tenant());

ALTER TABLE enrichment_cache ENABLE ROW LEVEL SECURITY;
ALTER TABLE enrichment_cache FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON enrichment_cache
  USING (tenant_id = app_tenant()) WITH CHECK (tenant_id = app_tenant());
