-- Phase 12: investigations (spec.md Layer 14). Append-only: an investigation is a record of
-- what the Investigator concluded about one verdict; proposals it makes live in
-- remediation_proposals and wait in the approval stage like any other proposal.

CREATE TABLE IF NOT EXISTS investigations (
  investigation_id     TEXT PRIMARY KEY,
  tenant_id            TEXT NOT NULL,
  verdict_id           TEXT NOT NULL REFERENCES verdicts (verdict_id),
  root_cause           TEXT NOT NULL,
  root_cause_category  TEXT NOT NULL CHECK (root_cause_category IN
                         ('offboarding_gap','missing_evidence','stale_evidence','configuration',
                          'ownership','identity_unresolved','unknown')),
  missing_evidence     JSONB NOT NULL DEFAULT '[]',
  proposed_actions     JSONB NOT NULL DEFAULT '[]',
  citations            JSONB NOT NULL DEFAULT '[]',
  confidence           DOUBLE PRECISION NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
  confidence_cap       DOUBLE PRECISION NOT NULL CHECK (confidence_cap >= 0 AND confidence_cap <= 1),
  model                TEXT,
  prompt_version       TEXT NOT NULL,
  guardrail_events     JSONB NOT NULL DEFAULT '[]',
  thread_id            TEXT,
  request_json         JSONB,
  response_json        JSONB,
  created_at           TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_investigations_verdict ON investigations (tenant_id, verdict_id);

GRANT SELECT, INSERT ON investigations TO {{app_role}};
REVOKE UPDATE, DELETE ON investigations FROM {{app_role}};
ALTER TABLE investigations ENABLE ROW LEVEL SECURITY;
ALTER TABLE investigations FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON investigations
  USING (tenant_id = app_tenant()) WITH CHECK (tenant_id = app_tenant());
