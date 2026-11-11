-- LT1 step 1: what code reads about a failing resource before anyone investigates it
-- (spec.md A-158).
--   lookups   insert-only: one read per failing resource and kind (AWS Config history in
--             7-day windows, CloudTrail events, a search for the resource's infrastructure
--             code), with its typed result and the raw responses it landed first. The reads
--             are chosen by code from the failing verdict, never by a model, and run in the
--             connector's process with the connector's read credentials.

CREATE TABLE IF NOT EXISTS lookups (
  tenant_id      TEXT NOT NULL,
  lookup_id      TEXT NOT NULL,
  verdict_id     TEXT NOT NULL,
  test_id        TEXT NOT NULL,
  resource       TEXT NOT NULL,                      -- the failing resource's name
  entity_id      TEXT,
  kind           TEXT NOT NULL CHECK (kind IN ('config_history', 'cloudtrail', 'infra_code')),
  params         JSONB NOT NULL,
  status         TEXT NOT NULL CHECK (status IN ('ok', 'empty', 'error', 'skipped')),
  result         JSONB NOT NULL,
  response_ids   JSONB NOT NULL,                     -- raw_responses rows, landed first
  error          TEXT,
  created_at     TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  PRIMARY KEY (tenant_id, lookup_id)
);
CREATE INDEX IF NOT EXISTS ix_lookups_verdict ON lookups (tenant_id, verdict_id);

GRANT SELECT, INSERT ON lookups TO {{app_role}};
REVOKE UPDATE, DELETE ON lookups FROM {{app_role}};
GRANT SELECT ON lookups TO {{agent_role}};  -- the get_lookups tool
ALTER TABLE lookups ENABLE ROW LEVEL SECURITY;
ALTER TABLE lookups FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON lookups
  USING (tenant_id = app_tenant()) WITH CHECK (tenant_id = app_tenant());

DROP TRIGGER IF EXISTS audit_chain ON lookups;
CREATE CONSTRAINT TRIGGER audit_chain AFTER INSERT ON lookups
  DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION audit_chain_append();
