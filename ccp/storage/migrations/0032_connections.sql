-- MT1 step 4: connections as data, checked daily (spec.md A-121).
--   connections        one row per version of a connection: a connector reading one provider
--                      account, from a fixture or live, with the purpose name of the credential
--                      it reads (never the secret); a change or a removal from the tenant's
--                      configuration is a new version, so the history says which account was
--                      connected when, and by what
--   connection_checks  one row per probe of a connection's credential: ok, auth (refused),
--                      not_visible (the account is hidden from it) or fail
-- Both are appended, never edited. A connection's health is derived from its newest check
-- and its sync runs, never stored.

CREATE TABLE IF NOT EXISTS connections (
  tenant_id      TEXT NOT NULL,
  connection_id  TEXT NOT NULL,
  version        INT NOT NULL CHECK (version >= 1),
  connector      TEXT NOT NULL CHECK (connector IN ('github', 'hris', 'manual')),
  source         TEXT NOT NULL CHECK (source IN ('fixture', 'live')),
  account        TEXT NOT NULL,
  credential     TEXT,
  enabled        BOOLEAN NOT NULL,
  recorded_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  recorded_by    TEXT NOT NULL,
  PRIMARY KEY (tenant_id, connection_id, version)
);

CREATE OR REPLACE VIEW current_connections WITH (security_invoker = true) AS
  SELECT DISTINCT ON (tenant_id, connection_id) *
  FROM connections ORDER BY tenant_id, connection_id, version DESC;

CREATE TABLE IF NOT EXISTS connection_checks (
  tenant_id      TEXT NOT NULL,
  check_id       TEXT NOT NULL,                     -- ULID
  connection_id  TEXT NOT NULL,
  checked_at     TIMESTAMPTZ NOT NULL,
  outcome        TEXT NOT NULL CHECK (outcome IN ('ok', 'auth', 'not_visible', 'fail')),
  detail         TEXT,
  PRIMARY KEY (tenant_id, check_id)
);
CREATE INDEX IF NOT EXISTS ix_connection_checks_latest
  ON connection_checks (tenant_id, connection_id, checked_at);
CREATE INDEX IF NOT EXISTS ix_sync_runs_connection
  ON sync_runs (tenant_id, connection_id, finished_at);

GRANT SELECT, INSERT ON connections, connection_checks TO {{app_role}};
REVOKE UPDATE, DELETE ON connections, connection_checks FROM {{app_role}};
GRANT SELECT ON current_connections TO {{app_role}};
ALTER TABLE connections ENABLE ROW LEVEL SECURITY;
ALTER TABLE connections FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON connections
  USING (tenant_id = app_tenant()) WITH CHECK (tenant_id = app_tenant());
ALTER TABLE connection_checks ENABLE ROW LEVEL SECURITY;
ALTER TABLE connection_checks FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON connection_checks
  USING (tenant_id = app_tenant()) WITH CHECK (tenant_id = app_tenant());
