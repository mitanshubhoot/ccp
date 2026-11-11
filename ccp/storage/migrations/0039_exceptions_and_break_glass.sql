-- MT4 step 3: exceptions, identity merges and break-glass go through the approval service
-- (spec.md A-132).
--   approvals.kind            three more kinds: exception, identity_merge, break_glass_review
--   approval_events.break_glass  an approval an admin made by break-glass
--   control_exceptions        insert-only: a failing subject accepted until a date, by whom
--                             and why, minted only by an approved exception item
--   exception_revocations     insert-only: an exception ended early, by whom and why
--   exception_status          each exception with its revocation, if any
--   review_resolutions.approval_id  the identity_merge item a resolution decided

ALTER TABLE approvals DROP CONSTRAINT IF EXISTS approvals_kind_check;
ALTER TABLE approvals ADD CONSTRAINT approvals_kind_check CHECK (kind IN
  ('tier3_interrupt', 'remediation', 'remediation_rollback', 'policy_draft', 'evidence_request',
   'exception', 'identity_merge', 'break_glass_review'));

ALTER TABLE approval_events ADD COLUMN IF NOT EXISTS break_glass BOOLEAN NOT NULL DEFAULT false;
CREATE INDEX IF NOT EXISTS ix_approval_events_break_glass
  ON approval_events (tenant_id, recorded_at) WHERE break_glass;

CREATE OR REPLACE VIEW approval_status WITH (security_invoker = true) AS
  SELECT a.approval_id, a.tenant_id, a.kind, a.ref_id, a.payload, a.payload_sha256, a.opened_at,
         e.event AS state, e.reviewer, e.note, e.recorded_at AS decided_at, e.decision,
         a.requested_by, a.expires_at, e.principal_id, e.break_glass
  FROM approvals a
  JOIN LATERAL (
    SELECT event, reviewer, note, recorded_at, decision, principal_id, break_glass
    FROM approval_events
    WHERE approval_id = a.approval_id ORDER BY recorded_at DESC, event_id DESC LIMIT 1
  ) e ON true;

ALTER TABLE review_resolutions ADD COLUMN IF NOT EXISTS approval_id TEXT;

CREATE TABLE IF NOT EXISTS control_exceptions (
  exception_id   TEXT PRIMARY KEY,                    -- ULID
  tenant_id      TEXT NOT NULL,
  approval_id    TEXT NOT NULL,
  test_id        TEXT NOT NULL,
  subject_key    TEXT NOT NULL,
  reason         TEXT NOT NULL CHECK (reason <> ''),
  owner          TEXT NOT NULL CHECK (owner <> ''),
  valid_until    TIMESTAMPTZ NOT NULL,
  granted_by     TEXT NOT NULL,                       -- the approver's principal id
  granted_at     TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  CHECK (valid_until > granted_at),
  CONSTRAINT ux_control_exceptions_tenant UNIQUE (tenant_id, exception_id),
  CONSTRAINT ux_control_exceptions_approval UNIQUE (tenant_id, approval_id),
  CONSTRAINT fk_control_exceptions_approval
    FOREIGN KEY (tenant_id, approval_id) REFERENCES approvals (tenant_id, approval_id)
);
CREATE INDEX IF NOT EXISTS ix_control_exceptions_subject
  ON control_exceptions (tenant_id, subject_key, valid_until);

CREATE TABLE IF NOT EXISTS exception_revocations (
  tenant_id     TEXT NOT NULL,
  exception_id  TEXT NOT NULL,
  revoked_by    TEXT NOT NULL,
  reason        TEXT NOT NULL CHECK (reason <> ''),
  revoked_at    TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  PRIMARY KEY (tenant_id, exception_id),               -- ended once
  CONSTRAINT fk_exception_revocations_exception
    FOREIGN KEY (tenant_id, exception_id) REFERENCES control_exceptions (tenant_id, exception_id)
);

CREATE OR REPLACE VIEW exception_status WITH (security_invoker = true) AS
  SELECT e.exception_id, e.tenant_id, e.approval_id, e.test_id, e.subject_key, e.reason,
         e.owner, e.valid_until, e.granted_by, e.granted_at,
         r.revoked_by, r.reason AS revoke_reason, r.revoked_at
  FROM control_exceptions e
  LEFT JOIN exception_revocations r
    ON r.tenant_id = e.tenant_id AND r.exception_id = e.exception_id;

GRANT SELECT, INSERT ON control_exceptions, exception_revocations TO {{app_role}};
REVOKE UPDATE, DELETE ON control_exceptions, exception_revocations FROM {{app_role}};
GRANT SELECT ON exception_status TO {{app_role}};
-- the agents' tools say "fail (excepted until D)" too (A-115: read only)
GRANT SELECT ON control_exceptions, exception_revocations, exception_status TO {{agent_role}};

ALTER TABLE control_exceptions ENABLE ROW LEVEL SECURITY;
ALTER TABLE control_exceptions FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON control_exceptions
  USING (tenant_id = app_tenant()) WITH CHECK (tenant_id = app_tenant());
ALTER TABLE exception_revocations ENABLE ROW LEVEL SECURITY;
ALTER TABLE exception_revocations FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON exception_revocations
  USING (tenant_id = app_tenant()) WITH CHECK (tenant_id = app_tenant());
