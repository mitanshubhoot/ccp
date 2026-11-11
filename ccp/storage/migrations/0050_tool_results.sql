-- MT10 step 4: tool results swapped out of an agent's context (spec.md A-153).
--   tool_results   insert-only: a tool result too large for the context, or swapped out to
--                  keep a call under its ceiling, kept whole under a handle; the model sees
--                  a preview and the handle, and reads more with the read_result tool, which
--                  runs as the read-only agent role like every tool

CREATE TABLE IF NOT EXISTS tool_results (
  tenant_id   TEXT NOT NULL,
  handle      TEXT NOT NULL,
  thread_id   TEXT NOT NULL,
  tool        TEXT NOT NULL,
  content     TEXT NOT NULL,
  chars       INTEGER NOT NULL,
  tokens      INTEGER NOT NULL,
  created_at  TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  PRIMARY KEY (tenant_id, handle),
  CHECK (split_part(thread_id, ':', 1) = tenant_id)
);
CREATE INDEX IF NOT EXISTS ix_tool_results_thread ON tool_results (tenant_id, thread_id);

GRANT SELECT, INSERT ON tool_results TO {{app_role}};
REVOKE UPDATE, DELETE ON tool_results FROM {{app_role}};
GRANT SELECT ON tool_results TO {{agent_role}};
ALTER TABLE tool_results ENABLE ROW LEVEL SECURITY;
ALTER TABLE tool_results FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON tool_results
  USING (tenant_id = app_tenant()) WITH CHECK (tenant_id = app_tenant());
