-- LT4 step 2: a time-boxed, read-only auditor workspace, and the record of everything CCP
-- asserts outside itself under a versioned publication policy (spec.md A-169).
--   auditor_grants       insert-only: one auditor's read access to named packages until a
--                        time, granted by a tenant admin
--   auditor_revocations  insert-only: a grant ended early
--   auditor_access       insert-only: every file an auditor read, and when
--   attestations         insert-only: one assertion that leaves CCP (a package shared, a
--                        questionnaire sent): the policy version it was judged under, whether
--                        the policy allowed it, why not, and the digest of what left

CREATE TABLE IF NOT EXISTS auditor_grants (
  tenant_id     TEXT NOT NULL,
  grant_id      TEXT NOT NULL,
  principal_id  TEXT NOT NULL,
  package_ids   JSONB NOT NULL,
  expires_at    TIMESTAMPTZ NOT NULL,
  granted_by    TEXT NOT NULL,
  note          TEXT NOT NULL DEFAULT '',
  created_at    TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  PRIMARY KEY (tenant_id, grant_id)
);
CREATE INDEX IF NOT EXISTS ix_auditor_grants_principal ON auditor_grants (tenant_id, principal_id);

CREATE TABLE IF NOT EXISTS auditor_revocations (
  tenant_id   TEXT NOT NULL,
  grant_id    TEXT NOT NULL,
  revoked_by  TEXT NOT NULL,
  reason      TEXT NOT NULL,
  revoked_at  TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  PRIMARY KEY (tenant_id, grant_id)
);

CREATE TABLE IF NOT EXISTS auditor_access (
  tenant_id     TEXT NOT NULL,
  access_id     TEXT NOT NULL,
  grant_id      TEXT NOT NULL,
  principal_id  TEXT NOT NULL,
  package_id    TEXT NOT NULL,
  file          TEXT NOT NULL,
  read_at       TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  PRIMARY KEY (tenant_id, access_id)
);

CREATE TABLE IF NOT EXISTS attestations (
  tenant_id       TEXT NOT NULL,
  attestation_id  TEXT NOT NULL,
  kind            TEXT NOT NULL,
  ref             TEXT NOT NULL,
  policy_version  TEXT NOT NULL,
  allowed         BOOLEAN NOT NULL,
  reasons         JSONB NOT NULL,
  items           INT NOT NULL,
  content_sha256  TEXT NOT NULL,
  asserted_by     TEXT NOT NULL,
  asserted_at     TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  PRIMARY KEY (tenant_id, attestation_id)
);

GRANT SELECT, INSERT ON auditor_grants, auditor_revocations, auditor_access, attestations
  TO {{app_role}};
REVOKE UPDATE, DELETE ON auditor_grants, auditor_revocations, auditor_access, attestations
  FROM {{app_role}};
ALTER TABLE auditor_grants ENABLE ROW LEVEL SECURITY;
ALTER TABLE auditor_grants FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON auditor_grants
  USING (tenant_id = app_tenant()) WITH CHECK (tenant_id = app_tenant());
ALTER TABLE auditor_revocations ENABLE ROW LEVEL SECURITY;
ALTER TABLE auditor_revocations FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON auditor_revocations
  USING (tenant_id = app_tenant()) WITH CHECK (tenant_id = app_tenant());
ALTER TABLE auditor_access ENABLE ROW LEVEL SECURITY;
ALTER TABLE auditor_access FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON auditor_access
  USING (tenant_id = app_tenant()) WITH CHECK (tenant_id = app_tenant());
ALTER TABLE attestations ENABLE ROW LEVEL SECURITY;
ALTER TABLE attestations FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON attestations
  USING (tenant_id = app_tenant()) WITH CHECK (tenant_id = app_tenant());

DROP TRIGGER IF EXISTS audit_chain ON auditor_grants;
CREATE CONSTRAINT TRIGGER audit_chain AFTER INSERT ON auditor_grants
  DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION audit_chain_append();
DROP TRIGGER IF EXISTS audit_chain ON auditor_revocations;
CREATE CONSTRAINT TRIGGER audit_chain AFTER INSERT ON auditor_revocations
  DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION audit_chain_append();
DROP TRIGGER IF EXISTS audit_chain ON attestations;
CREATE CONSTRAINT TRIGGER audit_chain AFTER INSERT ON attestations
  DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION audit_chain_append();
