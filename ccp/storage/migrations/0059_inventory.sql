-- LT11: the cloud inventory coverage is measured over, and what playbooks found where no
-- deterministic test covers an intent (spec.md A-166, A-167).
--   resource_inventory  insert-only: one snapshot of an account's resources from AWS
--                       Config, whole or not (a recorder that is off gives an incomplete
--                       snapshot, never an empty whole one)
--   playbook_results    insert-only: one service playbook's reading of one resource for
--                       one intent: fail or needs_review, never pass, with the JSON paths
--                       it rests on and the raw responses it landed first

CREATE TABLE IF NOT EXISTS resource_inventory (
  tenant_id     TEXT NOT NULL,
  snapshot_id   TEXT NOT NULL,
  account       TEXT NOT NULL,
  complete      BOOLEAN NOT NULL,
  resources     JSONB NOT NULL,
  detail        TEXT NOT NULL DEFAULT '',
  response_ids  JSONB NOT NULL,
  taken_at      TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  PRIMARY KEY (tenant_id, snapshot_id)
);
CREATE INDEX IF NOT EXISTS ix_resource_inventory_account
  ON resource_inventory (tenant_id, account, taken_at);

CREATE TABLE IF NOT EXISTS playbook_results (
  tenant_id      TEXT NOT NULL,
  result_id      TEXT NOT NULL,
  account        TEXT NOT NULL,
  resource_type  TEXT NOT NULL,
  resource_id    TEXT NOT NULL,
  service_class  TEXT NOT NULL,
  intent         TEXT NOT NULL,
  status         TEXT NOT NULL CHECK (status IN ('fail', 'needs_review')),
  reason         TEXT NOT NULL,
  paths          JSONB NOT NULL,
  playbook       TEXT NOT NULL,
  model          TEXT,
  response_ids   JSONB NOT NULL,
  created_at     TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  PRIMARY KEY (tenant_id, result_id)
);
CREATE INDEX IF NOT EXISTS ix_playbook_results_resource
  ON playbook_results (tenant_id, resource_type, resource_id, intent, created_at);

GRANT SELECT, INSERT ON resource_inventory, playbook_results TO {{app_role}};
REVOKE UPDATE, DELETE ON resource_inventory, playbook_results FROM {{app_role}};
GRANT SELECT ON resource_inventory, playbook_results TO {{agent_role}};
ALTER TABLE resource_inventory ENABLE ROW LEVEL SECURITY;
ALTER TABLE resource_inventory FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON resource_inventory
  USING (tenant_id = app_tenant()) WITH CHECK (tenant_id = app_tenant());
ALTER TABLE playbook_results ENABLE ROW LEVEL SECURITY;
ALTER TABLE playbook_results FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON playbook_results
  USING (tenant_id = app_tenant()) WITH CHECK (tenant_id = app_tenant());

DROP TRIGGER IF EXISTS audit_chain ON resource_inventory;
CREATE CONSTRAINT TRIGGER audit_chain AFTER INSERT ON resource_inventory
  DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION audit_chain_append();
DROP TRIGGER IF EXISTS audit_chain ON playbook_results;
CREATE CONSTRAINT TRIGGER audit_chain AFTER INSERT ON playbook_results
  DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION audit_chain_append();
