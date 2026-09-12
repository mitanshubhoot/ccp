-- Phase 7a: evaluation bookkeeping (spec.md Layer 7, A-11).
--   verdicts.trigger_reason  why the evaluation ran: impact | full_run | revalidate_interval |
--                            stale_evidence | reevaluate; the state writer maps stale triggers
--                            to reason = stale_evidence.
--   spend_ledger             append-only model spend per tenant, behind the daily budget gate.

ALTER TABLE verdicts ADD COLUMN IF NOT EXISTS trigger_reason TEXT NOT NULL DEFAULT 'impact';

CREATE TABLE IF NOT EXISTS spend_ledger (
  entry_id      TEXT PRIMARY KEY,
  tenant_id     TEXT NOT NULL,
  step_id       TEXT,
  usd           DOUBLE PRECISION NOT NULL CHECK (usd >= 0),
  tokens        INT NOT NULL DEFAULT 0,
  recorded_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_spend_tenant_day ON spend_ledger (tenant_id, recorded_at);

GRANT SELECT, INSERT ON spend_ledger TO {{app_role}};
ALTER TABLE spend_ledger ENABLE ROW LEVEL SECURITY;
ALTER TABLE spend_ledger FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON spend_ledger
  USING (tenant_id = app_tenant()) WITH CHECK (tenant_id = app_tenant());
