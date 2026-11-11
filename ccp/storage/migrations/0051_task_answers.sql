-- MT10 step 5: a person's answer to a waiting item (spec.md A-154).
--   task_answers   insert-only, on the audit chain: the question as it was asked, the answer
--                  as given, who gave it and when. A new run reads it as quoted, untrusted
--                  text; it is never evidence and never an approval

CREATE TABLE IF NOT EXISTS task_answers (
  tenant_id    TEXT NOT NULL,
  answer_id    TEXT NOT NULL,
  task_id      TEXT NOT NULL,
  item_id      TEXT NOT NULL,
  question     TEXT NOT NULL,
  answer       TEXT NOT NULL,
  answered_by  TEXT NOT NULL,
  answered_at  TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  PRIMARY KEY (tenant_id, answer_id)
);
CREATE INDEX IF NOT EXISTS ix_task_answers_item ON task_answers (tenant_id, task_id, item_id, answered_at);

GRANT SELECT, INSERT ON task_answers TO {{app_role}};
REVOKE UPDATE, DELETE ON task_answers FROM {{app_role}};
ALTER TABLE task_answers ENABLE ROW LEVEL SECURITY;
ALTER TABLE task_answers FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON task_answers
  USING (tenant_id = app_tenant()) WITH CHECK (tenant_id = app_tenant());

DROP TRIGGER IF EXISTS audit_chain ON task_answers;
CREATE CONSTRAINT TRIGGER audit_chain AFTER INSERT ON task_answers
  DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION audit_chain_append();
