-- MT8 step 6: rule changes, amendments by default and corrections by decision, with
-- preview and notice (spec.md A-128).
--   compliance_state.reason   adds rule_change: a subject re-decided under a new rule stamp,
--                             valid no earlier than that stamp's first verdict
--   compliance_state.catalogue_version   the catalogue in force when the row was written
--   rule_changes              the notice of every rule change applied: the test, the rule
--                             stamps before and after, correction or amendment, who decided
--                             and who signed off, and each subject's status before and after

ALTER TABLE compliance_state DROP CONSTRAINT IF EXISTS compliance_state_reason_check;
ALTER TABLE compliance_state ADD CONSTRAINT compliance_state_reason_check CHECK (
  reason IN ('evaluation', 'correction', 'human_override', 'late_event', 'stale_evidence',
             'rule_change')
);
ALTER TABLE compliance_state ADD COLUMN IF NOT EXISTS catalogue_version TEXT;

CREATE TABLE IF NOT EXISTS rule_changes (
  tenant_id        TEXT NOT NULL,
  change_id        TEXT NOT NULL,                    -- ULID
  test_id          TEXT NOT NULL,
  from_stamps      JSONB NOT NULL,                   -- the rule stamps it replaces
  to_stamp         TEXT NOT NULL,
  kind             TEXT NOT NULL CHECK (kind IN ('correction', 'amendment')),
  effective_from   TIMESTAMPTZ,                      -- an amendment's; a correction has none
  decided_at       TIMESTAMPTZ NOT NULL,
  decided_by       TEXT NOT NULL,
  signed_off_by    TEXT,                             -- named for a FAIL-to-PASS flip
  subjects         JSONB NOT NULL,                   -- [{subject_key, before, after}]
  notice           TEXT NOT NULL,
  PRIMARY KEY (tenant_id, change_id)
);
-- one notice per test, rule stamp and kind: the writer's amendment notice is appended once
CREATE UNIQUE INDEX IF NOT EXISTS ux_rule_changes_once
  ON rule_changes (tenant_id, test_id, to_stamp, kind);

GRANT SELECT, INSERT ON rule_changes TO {{app_role}};
REVOKE UPDATE, DELETE ON rule_changes FROM {{app_role}};
ALTER TABLE rule_changes ENABLE ROW LEVEL SECURITY;
ALTER TABLE rule_changes FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON rule_changes
  USING (tenant_id = app_tenant()) WITH CHECK (tenant_id = app_tenant());
