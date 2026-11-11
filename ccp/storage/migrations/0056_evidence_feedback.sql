-- LT2 step 1: segments a model cites by id, and code's feedback on every upload
-- (spec.md A-161).
--   segment_sets       insert-only: one document version's segments (sentences, headings,
--                      table cells, with page offsets), by the segmenter's version
--   evidence_feedback  insert-only: what code (and later the adequacy verifier, A-162) said
--                      about one document version: admissible or not, the provenance badge,
--                      the reason codes, and how long after landing it was written

CREATE TABLE IF NOT EXISTS segment_sets (
  tenant_id           TEXT NOT NULL,
  evidence_entity_id  TEXT NOT NULL,
  text_sha256         TEXT NOT NULL,
  version             TEXT NOT NULL,
  segments            JSONB NOT NULL,
  created_at          TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  PRIMARY KEY (tenant_id, evidence_entity_id, text_sha256, version)
);

CREATE TABLE IF NOT EXISTS evidence_feedback (
  tenant_id           TEXT NOT NULL,
  feedback_id         TEXT NOT NULL,
  evidence_entity_id  TEXT NOT NULL,
  source_event_id     TEXT NOT NULL,
  text_sha256         TEXT NOT NULL,
  kind                TEXT NOT NULL CHECK (kind IN ('admissibility', 'adequacy')),
  admissible          BOOLEAN,
  badge               TEXT NOT NULL CHECK (badge IN ('customer_attested', 'system_observed')),
  reasons             JSONB NOT NULL,
  detail              JSONB NOT NULL DEFAULT '{}'::jsonb,
  shown               BOOLEAN NOT NULL DEFAULT true,   -- false while a check runs in shadow
  seconds_after_landing DOUBLE PRECISION,
  created_at          TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  PRIMARY KEY (tenant_id, feedback_id)
);
CREATE INDEX IF NOT EXISTS ix_evidence_feedback_of
  ON evidence_feedback (tenant_id, evidence_entity_id, created_at);

GRANT SELECT, INSERT ON segment_sets, evidence_feedback TO {{app_role}};
REVOKE UPDATE, DELETE ON segment_sets, evidence_feedback FROM {{app_role}};
GRANT SELECT ON segment_sets, evidence_feedback TO {{agent_role}};
ALTER TABLE segment_sets ENABLE ROW LEVEL SECURITY;
ALTER TABLE segment_sets FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON segment_sets
  USING (tenant_id = app_tenant()) WITH CHECK (tenant_id = app_tenant());
ALTER TABLE evidence_feedback ENABLE ROW LEVEL SECURITY;
ALTER TABLE evidence_feedback FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON evidence_feedback
  USING (tenant_id = app_tenant()) WITH CHECK (tenant_id = app_tenant());

DROP TRIGGER IF EXISTS audit_chain ON evidence_feedback;
CREATE CONSTRAINT TRIGGER audit_chain AFTER INSERT ON evidence_feedback
  DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION audit_chain_append();
