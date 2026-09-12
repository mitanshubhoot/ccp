-- Phases 10 and 11: remediation proposals and the shared approval stage (spec.md Layers 10, 16).
--   remediation_proposals  operational: status, pre-state, and the exact call are updated in place
--   approvals              append-only; status is derived from the latest approval_events row

CREATE TABLE IF NOT EXISTS remediation_proposals (
  proposal_id     TEXT PRIMARY KEY,
  tenant_id       TEXT NOT NULL,
  verdict_id      TEXT NOT NULL REFERENCES verdicts (verdict_id),
  source          TEXT NOT NULL CHECK (source IN ('rule', 'investigator', 'copilot')),
  action          TEXT NOT NULL,
  target          JSONB NOT NULL,
  rationale       TEXT NOT NULL,
  reversible      BOOLEAN NOT NULL,
  live_capable    BOOLEAN NOT NULL,
  payload_sha256  TEXT NOT NULL,                       -- sha256(tenant, verdict, action, target)
  status          TEXT NOT NULL DEFAULT 'proposed' CHECK (status IN
                    ('proposed','dry_run','refused','simulated','executed','rolled_back','rejected')),
  pre_state       JSONB,
  planned_call    JSONB,                               -- the exact call a dry run would make
  executed_call   JSONB,
  refusal         TEXT,
  created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_proposals_verdict ON remediation_proposals (tenant_id, verdict_id);
GRANT SELECT, INSERT, UPDATE ON remediation_proposals TO {{app_role}};
ALTER TABLE remediation_proposals ENABLE ROW LEVEL SECURITY;
ALTER TABLE remediation_proposals FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON remediation_proposals
  USING (tenant_id = app_tenant()) WITH CHECK (tenant_id = app_tenant());

CREATE TABLE IF NOT EXISTS approvals (
  approval_id     TEXT PRIMARY KEY,
  tenant_id       TEXT NOT NULL,
  kind            TEXT NOT NULL CHECK (kind IN
                    ('tier3_interrupt','remediation','policy_draft','evidence_request')),
  ref_id          TEXT NOT NULL,                       -- thread id, proposal id, draft id, investigation id
  payload         JSONB NOT NULL,
  payload_sha256  TEXT NOT NULL,                       -- gate 5 in Layer 10 checks this
  opened_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_approvals_ref ON approvals (tenant_id, kind, ref_id);

CREATE TABLE IF NOT EXISTS approval_events (
  event_id        TEXT PRIMARY KEY,
  approval_id     TEXT NOT NULL REFERENCES approvals (approval_id),
  tenant_id       TEXT NOT NULL,
  event           TEXT NOT NULL CHECK (event IN
                    ('opened','approved','rejected','changes_requested','closed')),
  reviewer        TEXT NOT NULL CHECK (reviewer <> ''),
  note            TEXT,
  recorded_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_approval_events ON approval_events (approval_id, recorded_at);

GRANT SELECT, INSERT ON approvals, approval_events TO {{app_role}};
REVOKE UPDATE, DELETE ON approvals, approval_events FROM {{app_role}};

ALTER TABLE approvals ENABLE ROW LEVEL SECURITY;
ALTER TABLE approvals FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON approvals
  USING (tenant_id = app_tenant()) WITH CHECK (tenant_id = app_tenant());
ALTER TABLE approval_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE approval_events FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON approval_events
  USING (tenant_id = app_tenant()) WITH CHECK (tenant_id = app_tenant());

-- the latest event per approval is its state
CREATE OR REPLACE VIEW approval_status WITH (security_invoker = true) AS
  SELECT a.approval_id, a.tenant_id, a.kind, a.ref_id, a.payload, a.payload_sha256, a.opened_at,
         e.event AS state, e.reviewer, e.note, e.recorded_at AS decided_at
  FROM approvals a
  JOIN LATERAL (
    SELECT event, reviewer, note, recorded_at FROM approval_events
    WHERE approval_id = a.approval_id ORDER BY recorded_at DESC, event_id DESC LIMIT 1
  ) e ON true;
GRANT SELECT ON approval_status TO {{app_role}};
