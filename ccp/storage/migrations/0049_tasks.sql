-- MT10 step 1: long tasks as typed rows (spec.md A-150).
--   tasks        operational: one row per long task (audit preparation, a questionnaire's
--                open questions), its scope, its caps (dollars, deadline, runs per item)
--                and what it has spent; only the task runner changes it
--   task_items   operational: one row per item a code planner wrote from scoping facts;
--                status, attempts, the evidence it rests on, the open question, the last
--                run; only the runner changes an item's status, after its checks pass
--   task_events  insert-only, on the audit chain: every plan, status change, run, cap,
--                alert and park, so a reviewer can replay how the task went

CREATE TABLE IF NOT EXISTS tasks (
  tenant_id           TEXT NOT NULL,
  task_id             TEXT NOT NULL,
  kind                TEXT NOT NULL CHECK (kind IN ('audit_prep', 'questionnaire')),
  title               TEXT NOT NULL,
  scope               JSONB NOT NULL,
  status              TEXT NOT NULL DEFAULT 'open'
                        CHECK (status IN ('open', 'done', 'parked', 'cancelled')),
  parked_reason       TEXT,
  max_usd             NUMERIC(12, 6) NOT NULL CHECK (max_usd >= 0),
  forecast_usd        NUMERIC(12, 6) NOT NULL CHECK (forecast_usd >= 0),
  deadline            TIMESTAMPTZ NOT NULL,
  max_runs_per_item   INTEGER NOT NULL DEFAULT 3 CHECK (max_runs_per_item BETWEEN 1 AND 3),
  spent_usd           NUMERIC(12, 6) NOT NULL DEFAULT 0,
  passes              INTEGER NOT NULL DEFAULT 0,
  stalled_passes      INTEGER NOT NULL DEFAULT 0,
  alerted             BOOLEAN NOT NULL DEFAULT false,
  bundle_id           TEXT NOT NULL,
  created_by          TEXT NOT NULL,
  created_at          TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  updated_at          TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  PRIMARY KEY (tenant_id, task_id)
);
CREATE INDEX IF NOT EXISTS ix_tasks_status ON tasks (tenant_id, status, created_at);

CREATE TABLE IF NOT EXISTS task_items (
  tenant_id      TEXT NOT NULL,
  task_id        TEXT NOT NULL,
  item_id        TEXT NOT NULL,
  kind           TEXT NOT NULL CHECK (kind IN ('check', 'read')),
  question       TEXT NOT NULL,
  inputs         JSONB NOT NULL,
  status         TEXT NOT NULL DEFAULT 'open'
                   CHECK (status IN ('open', 'running', 'waiting', 'final', 'needs_review',
                                     'cancelled')),
  outcome        TEXT,
  attempts       INTEGER NOT NULL DEFAULT 0,
  evidence_ids   JSONB NOT NULL DEFAULT '[]'::jsonb,
  open_question  TEXT,
  last_run_id    TEXT,
  result         JSONB,
  created_at     TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  updated_at     TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  PRIMARY KEY (tenant_id, task_id, item_id),
  FOREIGN KEY (tenant_id, task_id) REFERENCES tasks (tenant_id, task_id)
);
CREATE INDEX IF NOT EXISTS ix_task_items_status ON task_items (tenant_id, task_id, status);

CREATE TABLE IF NOT EXISTS task_events (
  tenant_id    TEXT NOT NULL,
  event_id     TEXT NOT NULL,
  task_id      TEXT NOT NULL,
  item_id      TEXT,
  kind         TEXT NOT NULL,
  detail       JSONB NOT NULL DEFAULT '{}'::jsonb,
  run_id       TEXT,
  usd          NUMERIC(12, 6),
  recorded_at  TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  PRIMARY KEY (tenant_id, event_id)
);
CREATE INDEX IF NOT EXISTS ix_task_events_task ON task_events (tenant_id, task_id, recorded_at);
CREATE INDEX IF NOT EXISTS ix_task_events_kind ON task_events (tenant_id, kind, recorded_at);

GRANT SELECT, INSERT, UPDATE ON tasks, task_items TO {{app_role}};
REVOKE DELETE ON tasks, task_items FROM {{app_role}};
GRANT SELECT, INSERT ON task_events TO {{app_role}};
REVOKE UPDATE, DELETE ON task_events FROM {{app_role}};

ALTER TABLE tasks ENABLE ROW LEVEL SECURITY;
ALTER TABLE tasks FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON tasks
  USING (tenant_id = app_tenant()) WITH CHECK (tenant_id = app_tenant());
ALTER TABLE task_items ENABLE ROW LEVEL SECURITY;
ALTER TABLE task_items FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON task_items
  USING (tenant_id = app_tenant()) WITH CHECK (tenant_id = app_tenant());
ALTER TABLE task_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE task_events FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON task_events
  USING (tenant_id = app_tenant()) WITH CHECK (tenant_id = app_tenant());

DROP TRIGGER IF EXISTS audit_chain ON task_events;
CREATE CONSTRAINT TRIGGER audit_chain AFTER INSERT ON task_events
  DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION audit_chain_append();
