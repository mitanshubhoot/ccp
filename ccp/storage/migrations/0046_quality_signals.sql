-- MT7 step 2: production quality signals (spec.md A-143, A-144).
--   model_refusals      insert-only: a model refused an evaluation's request; with the test
--                       and the bundle, so each test's refusal rate is watched and gated
--   quality_checks      insert-only: the nightly judged sample, the daily canary and shadow
--                       runs, each with its bundle, what it measured and whether it passed

CREATE TABLE IF NOT EXISTS model_refusals (
  tenant_id    TEXT NOT NULL,
  refusal_id   TEXT NOT NULL,
  test_id      TEXT NOT NULL,
  bundle_id    TEXT NOT NULL,
  detail       TEXT NOT NULL,
  recorded_at  TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  PRIMARY KEY (tenant_id, refusal_id)
);
CREATE INDEX IF NOT EXISTS ix_model_refusals_test ON model_refusals (tenant_id, test_id, recorded_at);

CREATE TABLE IF NOT EXISTS quality_checks (
  tenant_id    TEXT NOT NULL,
  check_id     TEXT NOT NULL,
  kind         TEXT NOT NULL CHECK (kind IN ('judged_sample', 'canary', 'shadow')),
  bundle_id    TEXT NOT NULL,
  passed       BOOLEAN NOT NULL,
  measured     JSONB NOT NULL,
  recorded_at  TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  PRIMARY KEY (tenant_id, check_id)
);
CREATE INDEX IF NOT EXISTS ix_quality_checks_kind ON quality_checks (tenant_id, kind, recorded_at);

GRANT SELECT, INSERT ON model_refusals, quality_checks TO {{app_role}};
REVOKE UPDATE, DELETE ON model_refusals, quality_checks FROM {{app_role}};
ALTER TABLE model_refusals ENABLE ROW LEVEL SECURITY;
ALTER TABLE model_refusals FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON model_refusals
  USING (tenant_id = app_tenant()) WITH CHECK (tenant_id = app_tenant());
ALTER TABLE quality_checks ENABLE ROW LEVEL SECURITY;
ALTER TABLE quality_checks FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON quality_checks
  USING (tenant_id = app_tenant()) WITH CHECK (tenant_id = app_tenant());
