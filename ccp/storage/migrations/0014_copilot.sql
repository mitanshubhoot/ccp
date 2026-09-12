-- Phase 14: the Copilot's answers (spec.md Layer 17). Append-only, one row per turn; the
-- thread id carries the tenant prefix like every other checkpointed graph.

CREATE TABLE IF NOT EXISTS copilot_answers (
  answer_id         TEXT PRIMARY KEY,
  tenant_id         TEXT NOT NULL,
  thread_id         TEXT NOT NULL,
  turn              INTEGER NOT NULL CHECK (turn >= 1),
  question          TEXT NOT NULL,
  intent            TEXT NOT NULL CHECK (intent IN ('explain','status','missing','act','other')),
  text              TEXT NOT NULL,
  citations         JSONB NOT NULL DEFAULT '[]',
  requested_actions JSONB NOT NULL DEFAULT '[]',
  model             TEXT,
  prompt_version    TEXT NOT NULL,
  guardrail_events  JSONB NOT NULL DEFAULT '[]',
  latency_ms        DOUBLE PRECISION,
  first_token_ms    DOUBLE PRECISION,
  fence_nonce       TEXT,
  request_json      JSONB,
  response_json     JSONB,
  created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_copilot_answers_thread ON copilot_answers (tenant_id, thread_id, turn);

GRANT SELECT, INSERT ON copilot_answers TO {{app_role}};
REVOKE UPDATE, DELETE ON copilot_answers FROM {{app_role}};
ALTER TABLE copilot_answers ENABLE ROW LEVEL SECURITY;
ALTER TABLE copilot_answers FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON copilot_answers
  USING (tenant_id = app_tenant()) WITH CHECK (tenant_id = app_tenant());
