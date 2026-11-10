-- MT1: every pull is a sync run, recorded scope by scope (spec.md A-118).
--   sync_runs        one row per connector pull: what it landed, how it ended (complete,
--                    partial or failed) and, when it failed, the error's class and text
--   sync_run_scopes  one row per declared scope of the run: complete (every listing seen in
--                    full, so an absence there is a fact), partial, not_visible (the
--                    credential cannot see it) or failed, with a count and a detail
-- Both are appended, never edited; a connection's health is read from them (A-121).

CREATE TABLE IF NOT EXISTS sync_runs (
  run_id          TEXT NOT NULL,                     -- ULID
  tenant_id       TEXT NOT NULL,
  connection_id   TEXT,                              -- set once connections are data (A-121)
  connector       TEXT NOT NULL,
  source          TEXT NOT NULL,
  status          TEXT NOT NULL CHECK (status IN ('complete', 'partial', 'failed')),
  started_at      TIMESTAMPTZ NOT NULL,
  finished_at     TIMESTAMPTZ NOT NULL,
  events_landed   INT NOT NULL,
  duplicates      INT NOT NULL,
  error_class     TEXT,
  error           TEXT,
  PRIMARY KEY (tenant_id, run_id),
  CHECK (finished_at >= started_at)
);
CREATE INDEX IF NOT EXISTS ix_sync_runs_recent ON sync_runs (tenant_id, connector, finished_at);

CREATE TABLE IF NOT EXISTS sync_run_scopes (
  tenant_id   TEXT NOT NULL,
  run_id      TEXT NOT NULL,
  scope       TEXT NOT NULL,
  status      TEXT NOT NULL CHECK (status IN ('complete', 'partial', 'not_visible', 'failed')),
  items       INT NOT NULL DEFAULT 0,
  detail      TEXT,
  PRIMARY KEY (tenant_id, run_id, scope),
  FOREIGN KEY (tenant_id, run_id) REFERENCES sync_runs (tenant_id, run_id)
);

GRANT SELECT, INSERT ON sync_runs, sync_run_scopes TO {{app_role}};
REVOKE UPDATE, DELETE ON sync_runs, sync_run_scopes FROM {{app_role}};
ALTER TABLE sync_runs ENABLE ROW LEVEL SECURITY;
ALTER TABLE sync_runs FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON sync_runs
  USING (tenant_id = app_tenant()) WITH CHECK (tenant_id = app_tenant());
ALTER TABLE sync_run_scopes ENABLE ROW LEVEL SECURITY;
ALTER TABLE sync_run_scopes FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON sync_run_scopes
  USING (tenant_id = app_tenant()) WITH CHECK (tenant_id = app_tenant());
