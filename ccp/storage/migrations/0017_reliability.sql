-- NT4: reliability (spec.md A-97).
--   event_log.next_attempt_at     a retried record waits (exponential backoff with jitter)
--   event_log.deferrals           a throttle (429, 529, 503) defers without spending an attempt
--   ordered_failures              per-consumer failure state on ordered topics: a failing record
--                                 retries with backoff, then dead-letters, and the topic moves
--                                 on, so one bad record no longer stops a tenant's pipeline
--   dead_letter_replays           every replay of a dead record, appended, never edited
--   agent_threads.evaluation_key  a retried tier 3 evaluation resumes its own thread

ALTER TABLE event_log ADD COLUMN IF NOT EXISTS next_attempt_at TIMESTAMPTZ;
ALTER TABLE event_log ADD COLUMN IF NOT EXISTS deferrals INT NOT NULL DEFAULT 0;

CREATE TABLE IF NOT EXISTS ordered_failures (
  consumer        TEXT NOT NULL,                     -- the consumer's name, without the tenant
  "offset"        BIGINT NOT NULL REFERENCES event_log ("offset"),
  tenant_id       TEXT NOT NULL,
  topic           TEXT NOT NULL,
  attempts        INT NOT NULL DEFAULT 0,
  status          TEXT NOT NULL DEFAULT 'retrying'
                  CHECK (status IN ('retrying', 'dead', 'resolved')),
  last_error      TEXT,
  next_attempt_at TIMESTAMPTZ,
  updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (consumer, "offset")
);
CREATE INDEX IF NOT EXISTS ix_ordered_failures_status
  ON ordered_failures (tenant_id, consumer, status);
GRANT SELECT, INSERT, UPDATE ON ordered_failures TO {{app_role}};   -- operational state
ALTER TABLE ordered_failures ENABLE ROW LEVEL SECURITY;
ALTER TABLE ordered_failures FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON ordered_failures
  USING (tenant_id = app_tenant()) WITH CHECK (tenant_id = app_tenant());

CREATE TABLE IF NOT EXISTS dead_letter_replays (
  replay_id       TEXT PRIMARY KEY,
  tenant_id       TEXT NOT NULL,
  topic           TEXT NOT NULL,
  consumer        TEXT,                              -- ordered topics: who replayed it
  "offset"        BIGINT NOT NULL REFERENCES event_log ("offset"),
  outcome         TEXT NOT NULL CHECK (outcome IN ('requeued', 'done', 'failed')),
  detail          TEXT,
  replayed_by     TEXT NOT NULL CHECK (replayed_by <> ''),
  replayed_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
GRANT SELECT, INSERT ON dead_letter_replays TO {{app_role}};
REVOKE UPDATE, DELETE ON dead_letter_replays FROM {{app_role}};
ALTER TABLE dead_letter_replays ENABLE ROW LEVEL SECURITY;
ALTER TABLE dead_letter_replays FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON dead_letter_replays
  USING (tenant_id = app_tenant()) WITH CHECK (tenant_id = app_tenant());

ALTER TABLE agent_threads ADD COLUMN IF NOT EXISTS evaluation_key TEXT;
CREATE INDEX IF NOT EXISTS ix_agent_threads_key ON agent_threads (tenant_id, evaluation_key);
