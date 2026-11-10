-- MT2: schedules on each tenant's wall clock, fired once across processes (spec.md A-108).
--   schedules      a tenant's configured jobs: when each next falls due (next_run_at) and on
--                  which clock; a schedule dropped from the configuration is disabled, kept
--   schedule_runs  every firing, appended: the slot it was due for, how many later slots it
--                  folded in while nothing ran, and the job record it put on `jobs`. Keyed by
--                  (tenant, schedule, slot), so a slot cannot fire twice

CREATE TABLE IF NOT EXISTS schedules (
  tenant_id    TEXT NOT NULL,
  schedule_id  TEXT NOT NULL,
  job          TEXT NOT NULL,
  every        TEXT NOT NULL,
  time_zone    TEXT NOT NULL,
  next_run_at  TIMESTAMPTZ NOT NULL,
  last_run_at  TIMESTAMPTZ,
  enabled      BOOLEAN NOT NULL DEFAULT true,
  updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, schedule_id)
);
CREATE INDEX IF NOT EXISTS ix_schedules_due ON schedules (tenant_id, next_run_at) WHERE enabled;

CREATE TABLE IF NOT EXISTS schedule_runs (
  tenant_id    TEXT NOT NULL,
  schedule_id  TEXT NOT NULL,
  slot         TIMESTAMPTZ NOT NULL,
  missed       INT NOT NULL DEFAULT 0 CHECK (missed >= 0),
  job_offset   BIGINT NOT NULL,
  fired_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, schedule_id, slot),
  FOREIGN KEY (tenant_id, schedule_id) REFERENCES schedules (tenant_id, schedule_id),
  FOREIGN KEY (tenant_id, job_offset) REFERENCES event_log (tenant_id, "offset")
);

GRANT SELECT, INSERT, UPDATE ON schedules TO {{app_role}};     -- operational state
GRANT SELECT, INSERT ON schedule_runs TO {{app_role}};         -- appended, never edited
REVOKE UPDATE, DELETE ON schedule_runs FROM {{app_role}};

ALTER TABLE schedules ENABLE ROW LEVEL SECURITY;
ALTER TABLE schedules FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON schedules
  USING (tenant_id = app_tenant()) WITH CHECK (tenant_id = app_tenant());

ALTER TABLE schedule_runs ENABLE ROW LEVEL SECURITY;
ALTER TABLE schedule_runs FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON schedule_runs
  USING (tenant_id = app_tenant()) WITH CHECK (tenant_id = app_tenant());
