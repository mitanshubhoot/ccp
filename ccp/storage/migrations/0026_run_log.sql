-- MT2: the agent run log (spec.md A-109).
--   agent_steps          every model call, tool call and fence nonce of an agent run, written
--                        as it returns: the record a resume is served from and a replay is
--                        checked against. Appended, never edited.
--   agent_threads.input  the state a run started from, so a replay can start it again

CREATE TABLE IF NOT EXISTS agent_steps (
  step_id     TEXT PRIMARY KEY,                       -- ULID
  tenant_id   TEXT NOT NULL,
  thread_id   TEXT NOT NULL,                          -- the run
  seq         INT NOT NULL,                           -- order within the run
  node        TEXT NOT NULL,
  kind        TEXT NOT NULL CHECK (kind IN ('model', 'tool', 'nonce')),
  call_key    TEXT NOT NULL,                          -- request digest, tool_use id, node
  request     JSONB NOT NULL,
  response    JSONB NOT NULL,
  model       TEXT,
  usage       JSONB,
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (tenant_id, thread_id, kind, call_key),
  CHECK (split_part(thread_id, ':', 1) = tenant_id)
);
CREATE INDEX IF NOT EXISTS ix_agent_steps_run ON agent_steps (tenant_id, thread_id, seq);

GRANT SELECT, INSERT ON agent_steps TO {{app_role}};
REVOKE UPDATE, DELETE ON agent_steps FROM {{app_role}};
ALTER TABLE agent_steps ENABLE ROW LEVEL SECURITY;
ALTER TABLE agent_steps FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON agent_steps
  USING (tenant_id = app_tenant()) WITH CHECK (tenant_id = app_tenant());

ALTER TABLE agent_threads ADD COLUMN IF NOT EXISTS input JSONB;
