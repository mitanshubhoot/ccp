-- LT1 step 2: fix suggestions rendered by code, which a person applies (spec.md A-159).
--   fix_suggestions        insert-only: one suggested fix for one failing resource: the
--                          fix and its version, the hash of the templates it was rendered
--                          from, the checked parameters, what was rendered (a Terraform
--                          diff, commands, console steps), the offline checks it passed,
--                          and the hash that covers all of it
--   fix_suggestion_events  insert-only: each change of a suggestion's status (open,
--                          accepted, dismissed, confirmed, reopened, superseded), by whom
--                          and, for confirmed, the later verdict whose read saw the fix
--   fix_suggestion_status  the latest event laid over each suggestion

CREATE TABLE IF NOT EXISTS fix_suggestions (
  tenant_id         TEXT NOT NULL,
  suggestion_id     TEXT NOT NULL,
  verdict_id        TEXT NOT NULL,
  test_id           TEXT NOT NULL,
  resource          TEXT NOT NULL,
  source            TEXT NOT NULL CHECK (source IN ('code', 'model')),
  investigation_id  TEXT,
  fix               TEXT NOT NULL,
  fix_version       INT NOT NULL,
  template_sha256   TEXT NOT NULL,
  params            JSONB NOT NULL,
  body              JSONB NOT NULL,
  checks            JSONB NOT NULL,
  lookup_ids        JSONB NOT NULL,
  payload_sha256    TEXT NOT NULL,
  created_at        TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  PRIMARY KEY (tenant_id, suggestion_id)
);
CREATE INDEX IF NOT EXISTS ix_fix_suggestions_resource
  ON fix_suggestions (tenant_id, test_id, resource, fix);
CREATE INDEX IF NOT EXISTS ix_fix_suggestions_verdict ON fix_suggestions (tenant_id, verdict_id);

CREATE TABLE IF NOT EXISTS fix_suggestion_events (
  tenant_id      TEXT NOT NULL,
  event_id       TEXT NOT NULL,
  suggestion_id  TEXT NOT NULL,
  status         TEXT NOT NULL CHECK (status IN
                   ('open', 'accepted', 'dismissed', 'confirmed', 'reopened', 'superseded')),
  actor          TEXT NOT NULL,
  note           TEXT NOT NULL DEFAULT '',
  verdict_id     TEXT,
  recorded_at    TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  PRIMARY KEY (tenant_id, event_id)
);
CREATE INDEX IF NOT EXISTS ix_fix_suggestion_events_of
  ON fix_suggestion_events (tenant_id, suggestion_id, recorded_at);

CREATE OR REPLACE VIEW fix_suggestion_status WITH (security_invoker = true) AS
SELECT s.*, e.status, e.actor AS status_by, e.note AS status_note,
       e.verdict_id AS status_verdict_id, e.recorded_at AS status_at
FROM fix_suggestions s
JOIN LATERAL (
  SELECT status, actor, note, verdict_id, recorded_at FROM fix_suggestion_events x
  WHERE x.tenant_id = s.tenant_id AND x.suggestion_id = s.suggestion_id
  ORDER BY x.recorded_at DESC, x.event_id DESC LIMIT 1
) e ON true;

GRANT SELECT, INSERT ON fix_suggestions, fix_suggestion_events TO {{app_role}};
REVOKE UPDATE, DELETE ON fix_suggestions, fix_suggestion_events FROM {{app_role}};
GRANT SELECT ON fix_suggestion_status TO {{app_role}};
GRANT SELECT ON fix_suggestions, fix_suggestion_events, fix_suggestion_status TO {{agent_role}};
ALTER TABLE fix_suggestions ENABLE ROW LEVEL SECURITY;
ALTER TABLE fix_suggestions FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON fix_suggestions
  USING (tenant_id = app_tenant()) WITH CHECK (tenant_id = app_tenant());
ALTER TABLE fix_suggestion_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE fix_suggestion_events FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON fix_suggestion_events
  USING (tenant_id = app_tenant()) WITH CHECK (tenant_id = app_tenant());

DROP TRIGGER IF EXISTS audit_chain ON fix_suggestions;
CREATE CONSTRAINT TRIGGER audit_chain AFTER INSERT ON fix_suggestions
  DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION audit_chain_append();
DROP TRIGGER IF EXISTS audit_chain ON fix_suggestion_events;
CREATE CONSTRAINT TRIGGER audit_chain AFTER INSERT ON fix_suggestion_events
  DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION audit_chain_append();
