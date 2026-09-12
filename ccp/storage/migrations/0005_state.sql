-- Phase 5: verdicts and the bitemporal compliance-state table (spec.md Layers 5, 7, 9).
-- Both append-only: no UPDATE or DELETE is ever granted. Corrections are new rows that
-- reference what they supersede. Transaction time is a per-tenant sequence stamped inside
-- the writer's lock, so recorded order equals commit order.

CREATE TABLE IF NOT EXISTS verdicts (
  verdict_id          TEXT PRIMARY KEY,               -- ULID
  evaluation_key      TEXT NOT NULL,                  -- sha256(tenant,test,subject,trigger,prompt_version,model)
  tenant_id           TEXT NOT NULL,
  test_id             TEXT NOT NULL,
  subject_key         TEXT NOT NULL,
  status              TEXT NOT NULL CHECK (status IN ('pass','fail','partial','needs_review')),
  reasoning           TEXT NOT NULL,
  citations           JSONB NOT NULL DEFAULT '[]',
  evidence_refs       JSONB NOT NULL DEFAULT '[]',
  confidence          DOUBLE PRECISION NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
  confidence_cap      DOUBLE PRECISION NOT NULL CHECK (confidence_cap >= 0 AND confidence_cap <= 1),
  model_confidence    DOUBLE PRECISION,
  tier                SMALLINT NOT NULL CHECK (tier IN (1, 2, 3)),
  model               TEXT,
  prompt_version      TEXT,
  retrieved_chunk_ids JSONB NOT NULL DEFAULT '[]',
  guardrail_events    JSONB NOT NULL DEFAULT '[]',
  extracted_fields    JSONB,
  chunker_version     TEXT,
  schema_version      TEXT,
  fence_nonce         TEXT,
  trigger_observed_at TIMESTAMPTZ NOT NULL,
  valid_from          TIMESTAMPTZ NOT NULL,           -- derived by the test's valid_from strategy
  request_json        JSONB,                          -- the exact model request, for replay
  response_json       JSONB,                          -- the exact model response
  created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_verdicts_evaluation_key ON verdicts (evaluation_key);
CREATE INDEX IF NOT EXISTS ix_verdicts_subject ON verdicts (tenant_id, subject_key, created_at DESC);

CREATE TABLE IF NOT EXISTS tenant_sequences (       -- per-tenant monotonic counters
  tenant_id   TEXT NOT NULL,
  name        TEXT NOT NULL,
  value       BIGINT NOT NULL DEFAULT 0,
  PRIMARY KEY (tenant_id, name)
);

CREATE TABLE IF NOT EXISTS compliance_state (
  row_id            TEXT PRIMARY KEY,                 -- ULID
  tenant_id         TEXT NOT NULL,
  subject_key       TEXT NOT NULL,                    -- f"{test_id}|{scope}"
  test_id           TEXT NOT NULL,
  control_ids       JSONB NOT NULL DEFAULT '[]',      -- denormalized from the knowledge graph at write time
  status            TEXT NOT NULL CHECK (status IN ('pass','fail','partial','needs_review')),
  verdict_id        TEXT NOT NULL REFERENCES verdicts (verdict_id),
  valid_from        TIMESTAMPTZ NOT NULL,
  valid_to          TIMESTAMPTZ,                      -- NULL = open-ended as far as this row knows
  recorded_seq      BIGINT NOT NULL,                  -- per-tenant, stamped inside the writer's lock
  recorded_at       TIMESTAMPTZ NOT NULL,             -- transaction time, for display; ordered like recorded_seq
  supersedes_row_id TEXT REFERENCES compliance_state (row_id),
  reason            TEXT NOT NULL CHECK (reason IN ('evaluation','correction','human_override','late_event','stale_evidence')),
  CHECK (valid_to IS NULL OR valid_to > valid_from),
  UNIQUE (tenant_id, recorded_seq)
);
CREATE INDEX IF NOT EXISTS ix_cs_subject ON compliance_state (tenant_id, subject_key, recorded_seq DESC);
-- a row can be superseded at most once; a further correction supersedes the correction
CREATE UNIQUE INDEX IF NOT EXISTS ux_cs_supersedes ON compliance_state (supersedes_row_id)
  WHERE supersedes_row_id IS NOT NULL;

GRANT SELECT, INSERT ON verdicts, compliance_state TO {{app_role}};
GRANT SELECT, INSERT, UPDATE ON tenant_sequences TO {{app_role}};   -- an operational counter, not audit data
REVOKE UPDATE, DELETE ON verdicts, compliance_state FROM {{app_role}};

ALTER TABLE verdicts ENABLE ROW LEVEL SECURITY;
ALTER TABLE verdicts FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON verdicts
  USING (tenant_id = app_tenant()) WITH CHECK (tenant_id = app_tenant());

ALTER TABLE tenant_sequences ENABLE ROW LEVEL SECURITY;
ALTER TABLE tenant_sequences FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON tenant_sequences
  USING (tenant_id = app_tenant()) WITH CHECK (tenant_id = app_tenant());

ALTER TABLE compliance_state ENABLE ROW LEVEL SECURITY;
ALTER TABLE compliance_state FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON compliance_state
  USING (tenant_id = app_tenant()) WITH CHECK (tenant_id = app_tenant());
