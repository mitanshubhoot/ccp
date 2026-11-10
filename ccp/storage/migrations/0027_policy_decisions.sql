-- MT3: every tool call's permission decision, appended (spec.md A-113).
--   policy_decisions  one row per call attempt through the tool gateway: who asked (the
--                     principal: a graph type, or a caller of the API), for which run and user,
--                     which tool and class, a hash of the validated arguments, and what the
--                     check decided by which rule; a log-only rule that would have permitted a
--                     denied call is named too. Never edited.

CREATE TABLE IF NOT EXISTS policy_decisions (
  decision_id       TEXT PRIMARY KEY,                -- ULID
  tenant_id         TEXT NOT NULL,
  decided_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
  principal         TEXT NOT NULL,
  user_id           TEXT,
  run_id            TEXT,
  tool              TEXT NOT NULL,
  tool_class        TEXT NOT NULL,
  arguments_sha256  TEXT NOT NULL,
  effect            TEXT NOT NULL CHECK (effect IN ('permit', 'deny', 'ask')),
  rule_id           TEXT,
  shadow_rule_id    TEXT,
  reason            TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_policy_decisions_run ON policy_decisions (tenant_id, run_id, decided_at);

GRANT SELECT, INSERT ON policy_decisions TO {{app_role}};
REVOKE UPDATE, DELETE ON policy_decisions FROM {{app_role}};
ALTER TABLE policy_decisions ENABLE ROW LEVEL SECURITY;
ALTER TABLE policy_decisions FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON policy_decisions
  USING (tenant_id = app_tenant()) WITH CHECK (tenant_id = app_tenant());
