-- LT3: security questionnaires answered by a fixed pipeline, approved by a person, and
-- marked stale when what they rested on changes (spec.md A-163, A-164).
--   questionnaires         operational: one questionnaire, its questions, and whether a
--                          person has sent it
--   questionnaire_answers  insert-only: one drafted answer per question and run: every
--                          stage's output, the code checks, the pointers it cites and the
--                          verdicts it relied on
--   answer_events          insert-only: draft, approved (with the person's edit, if any),
--                          rejected, stale (with what changed) and sent
--   answer_library         insert-only: approved answers kept for reuse, with the same
--                          pointers and verdicts, re-checked before each reuse
--   answer_status          the latest event laid over each answer

CREATE TABLE IF NOT EXISTS questionnaires (
  tenant_id         TEXT NOT NULL,
  questionnaire_id  TEXT NOT NULL,
  title             TEXT NOT NULL,
  requester         TEXT NOT NULL DEFAULT '',
  questions         JSONB NOT NULL,
  status            TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'sent')),
  created_at        TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  sent_at           TIMESTAMPTZ,
  sent_by           TEXT,
  PRIMARY KEY (tenant_id, questionnaire_id)
);

CREATE TABLE IF NOT EXISTS questionnaire_answers (
  tenant_id         TEXT NOT NULL,
  answer_id         TEXT NOT NULL,
  questionnaire_id  TEXT NOT NULL,
  question_id       TEXT NOT NULL,
  question          TEXT NOT NULL,
  question_key      TEXT NOT NULL,
  control_id        TEXT,
  source            TEXT NOT NULL CHECK (source IN ('pipeline', 'library')),
  direction         TEXT NOT NULL,
  answer            TEXT NOT NULL,
  text              TEXT NOT NULL,
  stages            JSONB NOT NULL,
  checks            JSONB NOT NULL,
  pointers          JSONB NOT NULL,
  verdict_ids       JSONB NOT NULL,
  library_id        TEXT,
  model             TEXT,
  usd               DOUBLE PRECISION NOT NULL DEFAULT 0,
  created_at        TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  PRIMARY KEY (tenant_id, answer_id)
);
CREATE INDEX IF NOT EXISTS ix_questionnaire_answers_of
  ON questionnaire_answers (tenant_id, questionnaire_id, question_id, created_at);

CREATE TABLE IF NOT EXISTS answer_events (
  tenant_id    TEXT NOT NULL,
  event_id     TEXT NOT NULL,
  answer_id    TEXT NOT NULL,
  status       TEXT NOT NULL CHECK (status IN ('draft', 'approved', 'rejected', 'stale', 'sent')),
  actor        TEXT NOT NULL,
  note         TEXT NOT NULL DEFAULT '',
  edited_text  TEXT,
  recorded_at  TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  PRIMARY KEY (tenant_id, event_id)
);
CREATE INDEX IF NOT EXISTS ix_answer_events_of ON answer_events (tenant_id, answer_id, recorded_at);

CREATE TABLE IF NOT EXISTS answer_library (
  tenant_id       TEXT NOT NULL,
  library_id      TEXT NOT NULL,
  question_key    TEXT NOT NULL,
  question        TEXT NOT NULL,
  answer          TEXT NOT NULL,
  text            TEXT NOT NULL,
  pointers        JSONB NOT NULL,
  verdict_ids     JSONB NOT NULL,
  from_answer_id  TEXT NOT NULL,
  approved_by     TEXT NOT NULL,
  created_at      TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  PRIMARY KEY (tenant_id, library_id)
);
CREATE INDEX IF NOT EXISTS ix_answer_library_key ON answer_library (tenant_id, question_key, created_at);

CREATE OR REPLACE VIEW answer_status WITH (security_invoker = true) AS
SELECT a.*, e.status, e.actor AS status_by, e.note AS status_note, e.edited_text,
       e.recorded_at AS status_at
FROM questionnaire_answers a
JOIN LATERAL (
  SELECT status, actor, note, edited_text, recorded_at FROM answer_events x
  WHERE x.tenant_id = a.tenant_id AND x.answer_id = a.answer_id
  ORDER BY x.recorded_at DESC, x.event_id DESC LIMIT 1
) e ON true;

GRANT SELECT, INSERT, UPDATE ON questionnaires TO {{app_role}};
REVOKE DELETE ON questionnaires FROM {{app_role}};
GRANT SELECT, INSERT ON questionnaire_answers, answer_events, answer_library TO {{app_role}};
REVOKE UPDATE, DELETE ON questionnaire_answers, answer_events, answer_library FROM {{app_role}};
GRANT SELECT ON answer_status TO {{app_role}};

ALTER TABLE questionnaires ENABLE ROW LEVEL SECURITY;
ALTER TABLE questionnaires FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON questionnaires
  USING (tenant_id = app_tenant()) WITH CHECK (tenant_id = app_tenant());
ALTER TABLE questionnaire_answers ENABLE ROW LEVEL SECURITY;
ALTER TABLE questionnaire_answers FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON questionnaire_answers
  USING (tenant_id = app_tenant()) WITH CHECK (tenant_id = app_tenant());
ALTER TABLE answer_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE answer_events FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON answer_events
  USING (tenant_id = app_tenant()) WITH CHECK (tenant_id = app_tenant());
ALTER TABLE answer_library ENABLE ROW LEVEL SECURITY;
ALTER TABLE answer_library FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON answer_library
  USING (tenant_id = app_tenant()) WITH CHECK (tenant_id = app_tenant());

DROP TRIGGER IF EXISTS audit_chain ON answer_events;
CREATE CONSTRAINT TRIGGER audit_chain AFTER INSERT ON answer_events
  DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION audit_chain_append();
DROP TRIGGER IF EXISTS audit_chain ON answer_library;
CREATE CONSTRAINT TRIGGER audit_chain AFTER INSERT ON answer_library
  DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION audit_chain_append();
