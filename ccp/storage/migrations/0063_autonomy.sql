-- LT8 (narrowed): which drafts skip a person's review, and on what evidence (spec.md A-171).
--   autonomy_levels  insert-only: a class's level for a tenant and workflow, who set it (an
--                    administrator's opt-in, or CCP demoting it), the snapshot it rests on
--                    and the bundle that snapshot measured
--   auto_decisions   insert-only: one draft accepted without review, or sent to a person
--                    by the sample or the exposure budget, citing the snapshot

CREATE TABLE IF NOT EXISTS autonomy_levels (
  tenant_id       TEXT NOT NULL,
  level_id        TEXT NOT NULL,
  workflow        TEXT NOT NULL,
  decision_class  TEXT NOT NULL,
  level           TEXT NOT NULL CHECK (level IN ('shadow', 'suggest', 'approve_each', 'sampled')),
  snapshot        JSONB,
  bundle_id       TEXT,
  set_by          TEXT NOT NULL,
  reason          TEXT NOT NULL,
  set_at          TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  PRIMARY KEY (tenant_id, level_id)
);
CREATE INDEX IF NOT EXISTS ix_autonomy_levels_class
  ON autonomy_levels (tenant_id, workflow, decision_class, set_at);

CREATE TABLE IF NOT EXISTS auto_decisions (
  tenant_id       TEXT NOT NULL,
  decision_id     TEXT NOT NULL,
  workflow        TEXT NOT NULL,
  decision_class  TEXT NOT NULL,
  item_id         TEXT NOT NULL,
  outcome         TEXT NOT NULL CHECK (outcome IN ('auto', 'sampled_for_review', 'over_budget')),
  level_id        TEXT NOT NULL,
  snapshot        JSONB NOT NULL,
  decided_at      TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  PRIMARY KEY (tenant_id, decision_id)
);
CREATE INDEX IF NOT EXISTS ix_auto_decisions_day
  ON auto_decisions (tenant_id, workflow, decision_class, decided_at);

GRANT SELECT, INSERT ON autonomy_levels, auto_decisions TO {{app_role}};
REVOKE UPDATE, DELETE ON autonomy_levels, auto_decisions FROM {{app_role}};
ALTER TABLE autonomy_levels ENABLE ROW LEVEL SECURITY;
ALTER TABLE autonomy_levels FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON autonomy_levels
  USING (tenant_id = app_tenant()) WITH CHECK (tenant_id = app_tenant());
ALTER TABLE auto_decisions ENABLE ROW LEVEL SECURITY;
ALTER TABLE auto_decisions FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON auto_decisions
  USING (tenant_id = app_tenant()) WITH CHECK (tenant_id = app_tenant());

DROP TRIGGER IF EXISTS audit_chain ON autonomy_levels;
CREATE CONSTRAINT TRIGGER audit_chain AFTER INSERT ON autonomy_levels
  DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION audit_chain_append();
DROP TRIGGER IF EXISTS audit_chain ON auto_decisions;
CREATE CONSTRAINT TRIGGER audit_chain AFTER INSERT ON auto_decisions
  DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION audit_chain_append();
