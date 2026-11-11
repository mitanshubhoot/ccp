-- MT9 step 1: chunks as insert-only versions per evidence version, and index generations
-- switched only after the retrieval gate passes (spec.md A-145).
--
--   index_generations   insert-only: one per (embedding model, chunker version); the id is a
--                       content hash, so the same model and chunker always name the same one
--   index_activations   insert-only and chained: which generation serves from when, why
--                       ('first', 'gate_passed', 'forced') and the gate measurements
--   chunk_sets          insert-only: a generation's chunks of one evidence entity, written
--                       whenever they change, naming the evidence observation they came from;
--                       a tombstone (no observation, no chunks) when the evidence stops being
--                       current. As of a time, an entity's chunks are its newest set by then.
--   chunk_versions      insert-only: the chunks of a set, with character offsets into the
--                       evidence text (the page text for PDFs), so a quote can be read back
--                       from the source exactly
--   chunks              stays the serving projection of the active generation (operational,
--                       rebuilt from chunk_versions on a switch), now with its generation and
--                       offsets. Old generations' rows are kept for replay.

CREATE TABLE IF NOT EXISTS index_generations (
  tenant_id        TEXT NOT NULL,
  generation_id    TEXT NOT NULL,
  embed_model      TEXT NOT NULL,
  chunker_version  TEXT NOT NULL,
  created_at       TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  PRIMARY KEY (tenant_id, generation_id)
);

CREATE TABLE IF NOT EXISTS index_activations (
  tenant_id      TEXT NOT NULL,
  activation_id  TEXT NOT NULL,
  generation_id  TEXT NOT NULL,
  reason         TEXT NOT NULL CHECK (reason IN ('first', 'gate_passed', 'forced')),
  gate           JSONB NOT NULL,
  activated_at   TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  PRIMARY KEY (tenant_id, activation_id),
  FOREIGN KEY (tenant_id, generation_id) REFERENCES index_generations (tenant_id, generation_id)
);
CREATE INDEX IF NOT EXISTS ix_index_activations_at ON index_activations (tenant_id, activated_at);

CREATE TABLE IF NOT EXISTS chunk_sets (
  tenant_id             TEXT NOT NULL,
  set_id                TEXT NOT NULL,
  generation_id         TEXT NOT NULL,
  evidence_entity_id    TEXT NOT NULL,
  evidence_event_id     TEXT,                  -- the observation chunked; NULL: a tombstone
  evidence_observed_at  TIMESTAMPTZ,
  content_sha256        TEXT NOT NULL,         -- over the set's chunk ids and offsets
  chunk_count           INT NOT NULL,
  recorded_at           TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  PRIMARY KEY (tenant_id, set_id),
  FOREIGN KEY (tenant_id, generation_id) REFERENCES index_generations (tenant_id, generation_id)
);
CREATE INDEX IF NOT EXISTS ix_chunk_sets_latest
  ON chunk_sets (tenant_id, generation_id, evidence_entity_id, recorded_at DESC);

CREATE TABLE IF NOT EXISTS chunk_versions (
  tenant_id           TEXT NOT NULL,
  set_id              TEXT NOT NULL,
  chunk_id            TEXT NOT NULL,
  generation_id       TEXT NOT NULL,
  evidence_entity_id  TEXT NOT NULL,
  evidence_type       TEXT NOT NULL,
  subject_entity_id   TEXT,
  source_ref          TEXT NOT NULL,
  page                INT,
  ordinal             INT NOT NULL,
  start_offset        INT,                     -- [start, end) in the page or evidence text
  end_offset          INT,
  text                TEXT NOT NULL,
  context             TEXT,
  tsv                 tsvector GENERATED ALWAYS AS
                        (to_tsvector('english', coalesce(context, '') || ' ' || text)) STORED,
  embedding           vector(1024),
  embed_model         TEXT NOT NULL,
  chunker_version     TEXT NOT NULL,
  text_sha256         TEXT NOT NULL,
  PRIMARY KEY (tenant_id, set_id, chunk_id),
  FOREIGN KEY (tenant_id, set_id) REFERENCES chunk_sets (tenant_id, set_id)
);
CREATE INDEX IF NOT EXISTS ix_chunk_versions_chunk ON chunk_versions (tenant_id, chunk_id);
CREATE INDEX IF NOT EXISTS ix_chunk_versions_tsv ON chunk_versions USING gin (tsv);

ALTER TABLE chunks ADD COLUMN IF NOT EXISTS generation_id TEXT;
ALTER TABLE chunks ADD COLUMN IF NOT EXISTS start_offset INT;
ALTER TABLE chunks ADD COLUMN IF NOT EXISTS end_offset INT;

GRANT SELECT, INSERT ON index_generations, index_activations, chunk_sets, chunk_versions
  TO {{app_role}};
REVOKE UPDATE, DELETE ON index_generations, index_activations, chunk_sets, chunk_versions
  FROM {{app_role}};
GRANT SELECT ON index_generations, index_activations, chunk_sets, chunk_versions
  TO {{agent_role}};

ALTER TABLE index_generations ENABLE ROW LEVEL SECURITY;
ALTER TABLE index_generations FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON index_generations
  USING (tenant_id = app_tenant()) WITH CHECK (tenant_id = app_tenant());
ALTER TABLE index_activations ENABLE ROW LEVEL SECURITY;
ALTER TABLE index_activations FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON index_activations
  USING (tenant_id = app_tenant()) WITH CHECK (tenant_id = app_tenant());
ALTER TABLE chunk_sets ENABLE ROW LEVEL SECURITY;
ALTER TABLE chunk_sets FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON chunk_sets
  USING (tenant_id = app_tenant()) WITH CHECK (tenant_id = app_tenant());
ALTER TABLE chunk_versions ENABLE ROW LEVEL SECURITY;
ALTER TABLE chunk_versions FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON chunk_versions
  USING (tenant_id = app_tenant()) WITH CHECK (tenant_id = app_tenant());

-- a switch of the serving index is a decision: it joins the audit chain (A-137)
DROP TRIGGER IF EXISTS audit_chain ON index_activations;
CREATE CONSTRAINT TRIGGER audit_chain AFTER INSERT ON index_activations
  DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION audit_chain_append();
