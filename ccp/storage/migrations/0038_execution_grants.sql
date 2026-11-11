-- MT4 step 2b: a proposal is never edited, a decision mints one grant, and the executor
-- keeps a journal (spec.md A-131).
--   remediation_proposals  now insert-only: what was proposed, its code-written risk
--                          summary, the precondition it was proposed under and that
--                          precondition's hash, and for a rollback the proposal it reverses
--   proposal_events        insert-only: every change of a proposal's status, with the call,
--                          the pre-state and the refusal as of that change; proposal_status
--                          lays the latest one over the proposal
--   execution_grants       insert-only: one per approve decision on a remediation or a
--                          rollback item, bound to that item's payload and precondition
--                          hashes, valid for a day
--   grant_uses             insert-only: a grant's one use; the key holds back a second
--   execution_journal      insert-only: what the executor did on a grant: started (the call
--                          it will make), observed (the state it read first), then finished
--                          or failed, at most one of each per grant

ALTER TABLE remediation_proposals ADD CONSTRAINT ux_remediation_proposals_tenant
  UNIQUE (tenant_id, proposal_id);
ALTER TABLE remediation_proposals
  ADD COLUMN IF NOT EXISTS reverses TEXT,
  ADD COLUMN IF NOT EXISTS risk JSONB NOT NULL DEFAULT '{}'::jsonb,
  ADD COLUMN IF NOT EXISTS precondition JSONB NOT NULL DEFAULT '{}'::jsonb,
  ADD COLUMN IF NOT EXISTS precondition_sha256 TEXT NOT NULL DEFAULT '';
ALTER TABLE remediation_proposals ADD CONSTRAINT fk_remediation_proposals_reverses
  FOREIGN KEY (tenant_id, reverses) REFERENCES remediation_proposals (tenant_id, proposal_id);
ALTER TABLE remediation_proposals DROP CONSTRAINT IF EXISTS remediation_proposals_source_check;
ALTER TABLE remediation_proposals ADD CONSTRAINT remediation_proposals_source_check
  CHECK (source IN ('rule', 'investigator', 'copilot', 'rollback'));

CREATE TABLE IF NOT EXISTS proposal_events (
  event_id       TEXT PRIMARY KEY,                    -- ULID
  tenant_id      TEXT NOT NULL,
  proposal_id    TEXT NOT NULL,
  status         TEXT NOT NULL CHECK (status IN
                   ('dry_run', 'refused', 'simulated', 'executed', 'failed', 'rolled_back',
                    'rejected')),
  pre_state      JSONB,
  planned_call   JSONB,
  executed_call  JSONB,
  refusal        TEXT,
  recorded_at    TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  CONSTRAINT fk_proposal_events_proposal
    FOREIGN KEY (tenant_id, proposal_id) REFERENCES remediation_proposals (tenant_id, proposal_id)
);
CREATE INDEX IF NOT EXISTS ix_proposal_events_proposal
  ON proposal_events (tenant_id, proposal_id, recorded_at);

-- what each proposal held becomes its first event; then the columns edited in place go
INSERT INTO proposal_events (event_id, tenant_id, proposal_id, status, pre_state, planned_call,
                             executed_call, refusal, recorded_at)
  SELECT 'migrated:' || proposal_id, tenant_id, proposal_id, status, pre_state, planned_call,
         executed_call, refusal, updated_at
  FROM remediation_proposals WHERE status <> 'proposed'
  ON CONFLICT (event_id) DO NOTHING;
ALTER TABLE remediation_proposals
  DROP COLUMN IF EXISTS status,
  DROP COLUMN IF EXISTS pre_state,
  DROP COLUMN IF EXISTS planned_call,
  DROP COLUMN IF EXISTS executed_call,
  DROP COLUMN IF EXISTS refusal,
  DROP COLUMN IF EXISTS updated_at;

CREATE OR REPLACE VIEW proposal_status WITH (security_invoker = true) AS
  SELECT p.proposal_id, p.tenant_id, p.verdict_id, p.source, p.action, p.target, p.rationale,
         p.reversible, p.live_capable, p.payload_sha256,
         coalesce(e.status, 'proposed') AS status,
         e.pre_state, e.planned_call, e.executed_call, e.refusal,
         p.created_at, coalesce(e.recorded_at, p.created_at) AS updated_at,
         p.reverses, p.risk, p.precondition, p.precondition_sha256
  FROM remediation_proposals p
  LEFT JOIN LATERAL (
    SELECT status, pre_state, planned_call, executed_call, refusal, recorded_at
    FROM proposal_events
    WHERE tenant_id = p.tenant_id AND proposal_id = p.proposal_id
    ORDER BY recorded_at DESC, event_id DESC LIMIT 1
  ) e ON true;

CREATE TABLE IF NOT EXISTS execution_grants (
  grant_id             TEXT PRIMARY KEY,              -- ULID
  tenant_id            TEXT NOT NULL,
  approval_id          TEXT NOT NULL,
  proposal_id          TEXT NOT NULL,
  payload_sha256       TEXT NOT NULL,
  precondition_sha256  TEXT,
  issued_at            TIMESTAMPTZ NOT NULL,
  expires_at           TIMESTAMPTZ NOT NULL,
  CHECK (expires_at > issued_at),
  CONSTRAINT ux_execution_grants_tenant UNIQUE (tenant_id, grant_id),
  CONSTRAINT ux_execution_grants_approval UNIQUE (tenant_id, approval_id),  -- one per decision
  CONSTRAINT fk_execution_grants_approval
    FOREIGN KEY (tenant_id, approval_id) REFERENCES approvals (tenant_id, approval_id),
  CONSTRAINT fk_execution_grants_proposal
    FOREIGN KEY (tenant_id, proposal_id) REFERENCES remediation_proposals (tenant_id, proposal_id)
);

CREATE TABLE IF NOT EXISTS grant_uses (
  tenant_id   TEXT NOT NULL,
  grant_id    TEXT NOT NULL,
  operation   TEXT NOT NULL,                          -- proposal id and payload hash
  used_at     TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  PRIMARY KEY (tenant_id, grant_id),                  -- one use, whatever races
  CONSTRAINT fk_grant_uses_grant
    FOREIGN KEY (tenant_id, grant_id) REFERENCES execution_grants (tenant_id, grant_id)
);

CREATE TABLE IF NOT EXISTS execution_journal (
  entry_id     TEXT PRIMARY KEY,                      -- ULID
  tenant_id    TEXT NOT NULL,
  operation    TEXT NOT NULL,
  proposal_id  TEXT NOT NULL,
  grant_id     TEXT NOT NULL,
  entry        TEXT NOT NULL CHECK (entry IN ('started', 'observed', 'finished', 'failed')),
  mode         TEXT NOT NULL CHECK (mode IN ('simulated', 'live')),
  detail       JSONB NOT NULL DEFAULT '{}'::jsonb,
  recorded_at  TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  CONSTRAINT fk_execution_journal_grant
    FOREIGN KEY (tenant_id, grant_id) REFERENCES execution_grants (tenant_id, grant_id),
  CONSTRAINT fk_execution_journal_proposal
    FOREIGN KEY (tenant_id, proposal_id) REFERENCES remediation_proposals (tenant_id, proposal_id)
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_execution_journal_start
  ON execution_journal (tenant_id, grant_id) WHERE entry = 'started';
CREATE UNIQUE INDEX IF NOT EXISTS ux_execution_journal_end
  ON execution_journal (tenant_id, grant_id) WHERE entry IN ('finished', 'failed');
CREATE INDEX IF NOT EXISTS ix_execution_journal_proposal
  ON execution_journal (tenant_id, proposal_id, recorded_at);

ALTER TABLE credential_issues ADD COLUMN IF NOT EXISTS grant_id TEXT;

REVOKE UPDATE, DELETE ON remediation_proposals FROM {{app_role}};
GRANT SELECT ON proposal_status TO {{app_role}};
GRANT SELECT, INSERT ON proposal_events, execution_grants, grant_uses, execution_journal
  TO {{app_role}};
REVOKE UPDATE, DELETE ON proposal_events, execution_grants, grant_uses, execution_journal
  FROM {{app_role}};

ALTER TABLE proposal_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE proposal_events FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON proposal_events
  USING (tenant_id = app_tenant()) WITH CHECK (tenant_id = app_tenant());
ALTER TABLE execution_grants ENABLE ROW LEVEL SECURITY;
ALTER TABLE execution_grants FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON execution_grants
  USING (tenant_id = app_tenant()) WITH CHECK (tenant_id = app_tenant());
ALTER TABLE grant_uses ENABLE ROW LEVEL SECURITY;
ALTER TABLE grant_uses FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON grant_uses
  USING (tenant_id = app_tenant()) WITH CHECK (tenant_id = app_tenant());
ALTER TABLE execution_journal ENABLE ROW LEVEL SECURITY;
ALTER TABLE execution_journal FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON execution_journal
  USING (tenant_id = app_tenant()) WITH CHECK (tenant_id = app_tenant());
