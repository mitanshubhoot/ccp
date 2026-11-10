-- NT2: fail-closed approvals (spec.md Layer 16, A-95).
--   approval_events.decision   the exact decision a deciding event carries; a tier 3 resume
--                              reads it from here, so an unrecorded decision cannot be applied
--   remediation_rollback       a rollback waits for its own approval, bound to its payload
-- Append-only as before: the app role still has no UPDATE or DELETE on either table.

ALTER TABLE approval_events ADD COLUMN IF NOT EXISTS decision JSONB;

ALTER TABLE approvals DROP CONSTRAINT IF EXISTS approvals_kind_check;
ALTER TABLE approvals ADD CONSTRAINT approvals_kind_check CHECK (kind IN
  ('tier3_interrupt','remediation','remediation_rollback','policy_draft','evidence_request'));

-- the latest event per approval is its state; its decision travels with it
CREATE OR REPLACE VIEW approval_status WITH (security_invoker = true) AS
  SELECT a.approval_id, a.tenant_id, a.kind, a.ref_id, a.payload, a.payload_sha256, a.opened_at,
         e.event AS state, e.reviewer, e.note, e.recorded_at AS decided_at, e.decision
  FROM approvals a
  JOIN LATERAL (
    SELECT event, reviewer, note, recorded_at, decision FROM approval_events
    WHERE approval_id = a.approval_id ORDER BY recorded_at DESC, event_id DESC LIMIT 1
  ) e ON true;
GRANT SELECT ON approval_status TO {{app_role}};
