-- MT4 step 6: a person can stop an agent run (spec.md A-135).
--   run_stops            insert-only: a request to stop one run, by whom and why; the runner
--                        checks it at the next node boundary or tool call and ends the run
--   agent_threads.status a stopped run ends 'cancelled': it keeps its record and changes no
--                        status

CREATE TABLE IF NOT EXISTS run_stops (
  tenant_id     TEXT NOT NULL,
  thread_id     TEXT NOT NULL,
  requested_by  TEXT NOT NULL CHECK (requested_by <> ''),
  reason        TEXT,
  requested_at  TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  PRIMARY KEY (tenant_id, thread_id),                    -- a run is stopped once
  CHECK (split_part(thread_id, ':', 1) = tenant_id)
);

ALTER TABLE agent_threads DROP CONSTRAINT IF EXISTS agent_threads_status_check;
ALTER TABLE agent_threads ADD CONSTRAINT agent_threads_status_check
  CHECK (status IN ('running', 'interrupted', 'finished', 'failed', 'cancelled'));

GRANT SELECT, INSERT ON run_stops TO {{app_role}};
REVOKE UPDATE, DELETE ON run_stops FROM {{app_role}};
ALTER TABLE run_stops ENABLE ROW LEVEL SECURITY;
ALTER TABLE run_stops FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON run_stops
  USING (tenant_id = app_tenant()) WITH CHECK (tenant_id = app_tenant());
