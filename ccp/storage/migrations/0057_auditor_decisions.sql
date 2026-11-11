-- LT2 step 2: the adequacy verifier, checked against auditors before it is shown
-- (spec.md A-162).
--   auditor_decisions  insert-only: an auditor's accept-or-flag decision on one document
--                      version, the labels the verifier is calibrated against
-- and quality_checks gains the kind `adequacy`: the verifier against those decisions
-- (agreement, the false-accept upper bound), which must pass before its answers are shown.

CREATE TABLE IF NOT EXISTS auditor_decisions (
  tenant_id           TEXT NOT NULL,
  decision_id         TEXT NOT NULL,
  evidence_entity_id  TEXT NOT NULL,
  text_sha256         TEXT NOT NULL,
  decision            TEXT NOT NULL CHECK (decision IN ('accept', 'flag')),
  auditor             TEXT NOT NULL,
  note                TEXT NOT NULL DEFAULT '',
  created_at          TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  PRIMARY KEY (tenant_id, decision_id)
);
CREATE INDEX IF NOT EXISTS ix_auditor_decisions_of
  ON auditor_decisions (tenant_id, evidence_entity_id, text_sha256, created_at);

GRANT SELECT, INSERT ON auditor_decisions TO {{app_role}};
REVOKE UPDATE, DELETE ON auditor_decisions FROM {{app_role}};
ALTER TABLE auditor_decisions ENABLE ROW LEVEL SECURITY;
ALTER TABLE auditor_decisions FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON auditor_decisions
  USING (tenant_id = app_tenant()) WITH CHECK (tenant_id = app_tenant());

DROP TRIGGER IF EXISTS audit_chain ON auditor_decisions;
CREATE CONSTRAINT TRIGGER audit_chain AFTER INSERT ON auditor_decisions
  DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION audit_chain_append();

ALTER TABLE quality_checks DROP CONSTRAINT IF EXISTS quality_checks_kind_check;
ALTER TABLE quality_checks ADD CONSTRAINT quality_checks_kind_check
  CHECK (kind IN ('judged_sample', 'canary', 'shadow', 'relabel', 'adequacy'));
