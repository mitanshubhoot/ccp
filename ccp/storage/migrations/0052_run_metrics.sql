-- MT10 step 6: harness metrics per run (spec.md A-155).
--   run_metrics   insert-only: one row per finished agent run, derived from its run log and
--                 its guardrail events: how it stopped, its tool calls per tool (errors,
--                 repeats), its correction round and whether it worked, whether it passed
--                 the code checks first time, what it swapped or dropped to stay under its
--                 ceiling, its tokens and dollars, and whether its result was accepted.
--                 Grouped per graph and bundle, these gate a bundle (no rise in loop or cap
--                 stops) and say what a run costs per accepted result.

CREATE TABLE IF NOT EXISTS run_metrics (
  tenant_id             TEXT NOT NULL,
  thread_id             TEXT NOT NULL,
  graph                 TEXT NOT NULL,
  bundle_id             TEXT NOT NULL,
  stops                 JSONB NOT NULL,          -- every non-normal stop reason, in order
  model_calls           INTEGER NOT NULL,
  tool_calls            JSONB NOT NULL,          -- tool -> {calls, errors, repeated}
  corrections           INTEGER NOT NULL,
  correction_succeeded  BOOLEAN,                 -- NULL when no round ran
  first_pass            BOOLEAN NOT NULL,        -- every code check passed with no round
  context_swaps         INTEGER NOT NULL,
  context_dropped       INTEGER NOT NULL,
  tokens                INTEGER NOT NULL,
  usd                   NUMERIC(12, 6) NOT NULL,
  accepted              BOOLEAN NOT NULL,
  recorded_at           TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  PRIMARY KEY (tenant_id, thread_id, recorded_at),
  CHECK (split_part(thread_id, ':', 1) = tenant_id)
);
CREATE INDEX IF NOT EXISTS ix_run_metrics_graph ON run_metrics (tenant_id, graph, bundle_id, recorded_at);

GRANT SELECT, INSERT ON run_metrics TO {{app_role}};
REVOKE UPDATE, DELETE ON run_metrics FROM {{app_role}};
ALTER TABLE run_metrics ENABLE ROW LEVEL SECURITY;
ALTER TABLE run_metrics FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON run_metrics
  USING (tenant_id = app_tenant()) WITH CHECK (tenant_id = app_tenant());
