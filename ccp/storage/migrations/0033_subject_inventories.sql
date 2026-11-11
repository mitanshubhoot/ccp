-- MT8 step 2: expected subjects, and what the rollup may not call a pass (spec.md A-124).
--   subject_inventories  one row per test per full run: the subjects the test is expected
--                        to have a verdict for, and whether the source scopes they are
--                        listed from were seen whole and fresh when they were listed;
--                        appended, never edited, so the population a rollup judged is kept
--   parked_evaluations   the subject keys whose latest evaluation is parked (a model tier
--                        without a key, or the day's budget spent); a view over the queue
--                        that shows no ref, no payload and no other topic
-- The rollup reads both, through the application role and the agent role alike, so an
-- operator's report and an agent's answer judge the same population.

CREATE TABLE IF NOT EXISTS subject_inventories (
  tenant_id     TEXT NOT NULL,
  inventory_id  TEXT NOT NULL,                       -- ULID
  test_id       TEXT NOT NULL,
  computed_at   TIMESTAMPTZ NOT NULL,
  fresh_until   TIMESTAMPTZ NOT NULL,                -- after this, the population is stale
  subjects      JSONB NOT NULL,                      -- sorted subject keys, "TEST|scope"
  complete      BOOLEAN NOT NULL,                    -- every listing scope whole and fresh
  gaps          JSONB NOT NULL DEFAULT '[]',         -- the scopes that were not
  PRIMARY KEY (tenant_id, inventory_id)
);
CREATE INDEX IF NOT EXISTS ix_subject_inventories_latest
  ON subject_inventories (tenant_id, test_id, computed_at);

CREATE OR REPLACE VIEW parked_evaluations AS
  SELECT tenant_id, substr(partition_key, strpos(partition_key, ':') + 1) AS subject_key,
         produced_at
  FROM event_log
  WHERE topic = 'evaluations' AND status = 'parked' AND tenant_id = app_tenant();

GRANT SELECT, INSERT ON subject_inventories TO {{app_role}};
REVOKE UPDATE, DELETE ON subject_inventories FROM {{app_role}};
GRANT SELECT ON parked_evaluations TO {{app_role}};
GRANT SELECT ON subject_inventories, parked_evaluations TO {{agent_role}};
ALTER TABLE subject_inventories ENABLE ROW LEVEL SECURITY;
ALTER TABLE subject_inventories FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON subject_inventories
  USING (tenant_id = app_tenant()) WITH CHECK (tenant_id = app_tenant());
