-- LT4: as-of audit packages, the auditor workspace, and the record of everything CCP
-- asserted outside itself (spec.md A-168, A-169).
--   audit_packages  insert-only: one sealed package: its framework, the moment it is as of,
--                   the package it corrects (a delta), its signed manifest and where the
--                   sealed copy lies

CREATE TABLE IF NOT EXISTS audit_packages (
  tenant_id     TEXT NOT NULL,
  package_id    TEXT NOT NULL,
  framework_id  TEXT NOT NULL,
  valid_at      TIMESTAMPTZ NOT NULL,
  known_at      TIMESTAMPTZ NOT NULL,
  corrects      TEXT,
  manifest      JSONB NOT NULL,
  signature     TEXT NOT NULL,
  results       JSONB NOT NULL,           -- subject -> verdict id and status, for deltas
  sealed_refs   JSONB NOT NULL,
  created_by    TEXT NOT NULL,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  PRIMARY KEY (tenant_id, package_id)
);

GRANT SELECT, INSERT ON audit_packages TO {{app_role}};
REVOKE UPDATE, DELETE ON audit_packages FROM {{app_role}};
ALTER TABLE audit_packages ENABLE ROW LEVEL SECURITY;
ALTER TABLE audit_packages FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON audit_packages
  USING (tenant_id = app_tenant()) WITH CHECK (tenant_id = app_tenant());

DROP TRIGGER IF EXISTS audit_chain ON audit_packages;
CREATE CONSTRAINT TRIGGER audit_chain AFTER INSERT ON audit_packages
  DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION audit_chain_append();
