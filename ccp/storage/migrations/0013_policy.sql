-- Phase 13: the Policy Agent's outputs (spec.md Layer 15). Both append-only. A draft lives on
-- disk under data/drafts/ and is registered here with its approval item; it never becomes
-- evidence on its own.

CREATE TABLE IF NOT EXISTS gap_reports (
  report_id        TEXT PRIMARY KEY,
  tenant_id        TEXT NOT NULL,
  control_id       TEXT NOT NULL,
  gaps             JSONB NOT NULL DEFAULT '[]',
  summary          TEXT NOT NULL,
  confidence       DOUBLE PRECISION NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
  confidence_cap   DOUBLE PRECISION NOT NULL CHECK (confidence_cap >= 0 AND confidence_cap <= 1),
  model            TEXT,
  prompt_version   TEXT NOT NULL,
  guardrail_events JSONB NOT NULL DEFAULT '[]',
  thread_id        TEXT,
  request_json     JSONB,
  response_json    JSONB,
  created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_gap_reports_control ON gap_reports (tenant_id, control_id, created_at DESC);

CREATE TABLE IF NOT EXISTS policy_drafts (
  draft_id                 TEXT PRIMARY KEY,
  tenant_id                TEXT NOT NULL,
  control_id               TEXT NOT NULL,
  based_on_report_id       TEXT NOT NULL REFERENCES gap_reports (report_id),
  path                     TEXT NOT NULL,
  markdown_sha256          TEXT NOT NULL,
  addressed_requirements   JSONB NOT NULL DEFAULT '[]',
  approval_id              TEXT NOT NULL,
  scrub_events             JSONB NOT NULL DEFAULT '[]',
  created_at               TIMESTAMPTZ NOT NULL DEFAULT now()
);

GRANT SELECT, INSERT ON gap_reports, policy_drafts TO {{app_role}};
REVOKE UPDATE, DELETE ON gap_reports, policy_drafts FROM {{app_role}};
ALTER TABLE gap_reports ENABLE ROW LEVEL SECURITY;
ALTER TABLE gap_reports FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON gap_reports
  USING (tenant_id = app_tenant()) WITH CHECK (tenant_id = app_tenant());
ALTER TABLE policy_drafts ENABLE ROW LEVEL SECURITY;
ALTER TABLE policy_drafts FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON policy_drafts
  USING (tenant_id = app_tenant()) WITH CHECK (tenant_id = app_tenant());
