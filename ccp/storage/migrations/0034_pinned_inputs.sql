-- MT8 step 3: a tier-1 verdict can be re-derived from what it read (spec.md A-125).
--   recorded_seq   on every entity observation and identity link or review row: the
--                  tenant's `observations` counter, stamped under its lock at insert, so the
--                  rows at or below a watermark are exactly what was visible when the
--                  watermark was read. A row from before this migration has none and is
--                  always included.
--   manifest       on verdicts: what a tier-1 evaluation read (the watermark, its clock,
--                  the sync runs behind its required sources, the tenant parameters).
--   replay_checks  the nightly sentinel's re-derivations, appended.

ALTER TABLE persons ADD COLUMN IF NOT EXISTS recorded_seq BIGINT;
ALTER TABLE access_grants ADD COLUMN IF NOT EXISTS recorded_seq BIGINT;
ALTER TABLE assets ADD COLUMN IF NOT EXISTS recorded_seq BIGINT;
ALTER TABLE evidence ADD COLUMN IF NOT EXISTS recorded_seq BIGINT;
ALTER TABLE vendors ADD COLUMN IF NOT EXISTS recorded_seq BIGINT;
ALTER TABLE findings ADD COLUMN IF NOT EXISTS recorded_seq BIGINT;
ALTER TABLE person_links ADD COLUMN IF NOT EXISTS recorded_seq BIGINT;
ALTER TABLE review_queue ADD COLUMN IF NOT EXISTS recorded_seq BIGINT;
ALTER TABLE review_resolutions ADD COLUMN IF NOT EXISTS recorded_seq BIGINT;
ALTER TABLE verdicts ADD COLUMN IF NOT EXISTS manifest JSONB;

CREATE TABLE IF NOT EXISTS replay_checks (
  tenant_id    TEXT NOT NULL,
  check_id     TEXT NOT NULL,                        -- ULID
  verdict_id   TEXT NOT NULL,
  test_id      TEXT NOT NULL,
  checked_at   TIMESTAMPTZ NOT NULL,
  outcome      TEXT NOT NULL CHECK (outcome IN ('exact', 'rule_changed', 'mismatch')),
  diff         JSONB NOT NULL DEFAULT '[]',
  PRIMARY KEY (tenant_id, check_id)
);
CREATE INDEX IF NOT EXISTS ix_replay_checks_recent ON replay_checks (tenant_id, checked_at);

GRANT SELECT, INSERT ON replay_checks TO {{app_role}};
REVOKE UPDATE, DELETE ON replay_checks FROM {{app_role}};
ALTER TABLE replay_checks ENABLE ROW LEVEL SECURITY;
ALTER TABLE replay_checks FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON replay_checks
  USING (tenant_id = app_tenant()) WITH CHECK (tenant_id = app_tenant());
