-- MT4 step 2: decisions by signed-in members, one per item, expiring (spec.md A-130).
--   approvals.requested_by       who asked: a principal id, an agent, or the operator; a
--                                person never decides their own request
--   approvals.expires_at         after this no decision is taken, and nothing acts on it
--   approval_events.principal_id the signed-in member who decided, and the role they held
--   ux_approval_one_decision     one approve or reject per item, whatever races

ALTER TABLE approvals ADD COLUMN IF NOT EXISTS requested_by TEXT;
ALTER TABLE approvals ADD COLUMN IF NOT EXISTS expires_at TIMESTAMPTZ;
ALTER TABLE approval_events ADD COLUMN IF NOT EXISTS principal_id TEXT;
ALTER TABLE approval_events ADD COLUMN IF NOT EXISTS role TEXT;
CREATE UNIQUE INDEX IF NOT EXISTS ux_approval_one_decision
  ON approval_events (tenant_id, approval_id) WHERE event IN ('approved', 'rejected');

CREATE OR REPLACE VIEW approval_status WITH (security_invoker = true) AS
  SELECT a.approval_id, a.tenant_id, a.kind, a.ref_id, a.payload, a.payload_sha256, a.opened_at,
         e.event AS state, e.reviewer, e.note, e.recorded_at AS decided_at, e.decision,
         a.requested_by, a.expires_at, e.principal_id
  FROM approvals a
  JOIN LATERAL (
    SELECT event, reviewer, note, recorded_at, decision, principal_id FROM approval_events
    WHERE approval_id = a.approval_id ORDER BY recorded_at DESC, event_id DESC LIMIT 1
  ) e ON true;
