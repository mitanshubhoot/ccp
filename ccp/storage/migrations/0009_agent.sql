-- Phase 8: the agent runtime's own bookkeeping (spec.md Layer 8).
--   checkpoints schema  LangGraph's PostgresSaver owns the tables inside it; the migration
--                       runner calls setup() as the admin role after this file and then
--                       applies row-level security on thread_id (see ccp/storage/db.py).
--   agent_threads       one row per graph run the tier 3 wrapper starts: where it stopped,
--                       the interrupt payload a reviewer sees, and the verdict it produced.

CREATE SCHEMA IF NOT EXISTS checkpoints;
GRANT USAGE ON SCHEMA checkpoints TO {{app_role}};

CREATE TABLE IF NOT EXISTS agent_threads (
  thread_id     TEXT PRIMARY KEY,                     -- "{tenant}:{test}:{subject}:{run_id}"
  tenant_id     TEXT NOT NULL,
  graph         TEXT NOT NULL,
  test_id       TEXT,
  subject_key   TEXT,
  status        TEXT NOT NULL CHECK (status IN ('running','interrupted','finished','failed')),
  interrupt     JSONB,                                -- the gate's payload while interrupted
  decision      JSONB,                                -- the human decision that resumed it
  verdict_id    TEXT,
  error         TEXT,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  CHECK (thread_id LIKE tenant_id || ':%')
);
CREATE INDEX IF NOT EXISTS ix_agent_threads_status ON agent_threads (tenant_id, status, created_at);

GRANT SELECT, INSERT, UPDATE ON agent_threads TO {{app_role}};   -- operational, not audit data
ALTER TABLE agent_threads ENABLE ROW LEVEL SECURITY;
ALTER TABLE agent_threads FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON agent_threads
  USING (tenant_id = app_tenant()) WITH CHECK (tenant_id = app_tenant());
