-- MT4 step 4: tenants live in the database, versioned; a change of scope is approved
-- (spec.md A-133).
--   tenant_versions    insert-only: every version of a tenant's settings, its status, why it
--                      changed, who recorded it, and the scope_change item that allowed it
--   current_tenants    the latest version per tenant
--   tenant_directory   cross-tenant dispatch metadata (A-106): which tenants exist and since
--                      when, so the operator and the scheduler can list them; no settings
--   approvals.kind     scope_change: a change to what is tested waits for an approver

CREATE TABLE IF NOT EXISTS tenant_versions (
  tenant_id     TEXT NOT NULL,
  version_id    TEXT NOT NULL,                       -- ULID
  settings      JSONB NOT NULL,                      -- validated as TenantSettings before write
  status        TEXT NOT NULL CHECK (status IN ('active', 'suspended', 'offboarded')),
  reason        TEXT NOT NULL CHECK (reason <> ''),
  recorded_by   TEXT NOT NULL CHECK (recorded_by <> ''),
  approval_id   TEXT,                                -- the scope_change item, when one allowed it
  recorded_at   TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  PRIMARY KEY (tenant_id, version_id)
);
CREATE INDEX IF NOT EXISTS ix_tenant_versions_latest
  ON tenant_versions (tenant_id, recorded_at DESC, version_id DESC);

CREATE OR REPLACE VIEW current_tenants WITH (security_invoker = true) AS
  SELECT DISTINCT ON (tenant_id) *
  FROM tenant_versions ORDER BY tenant_id, recorded_at DESC, version_id DESC;

CREATE TABLE IF NOT EXISTS tenant_directory (
  tenant_id   TEXT PRIMARY KEY,
  created_at  TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);

ALTER TABLE approvals DROP CONSTRAINT IF EXISTS approvals_kind_check;
ALTER TABLE approvals ADD CONSTRAINT approvals_kind_check CHECK (kind IN
  ('tier3_interrupt', 'remediation', 'remediation_rollback', 'policy_draft', 'evidence_request',
   'exception', 'identity_merge', 'break_glass_review', 'scope_change'));

GRANT SELECT, INSERT ON tenant_versions TO {{app_role}};
REVOKE UPDATE, DELETE ON tenant_versions FROM {{app_role}};
GRANT SELECT ON current_tenants TO {{app_role}};
GRANT SELECT, INSERT ON tenant_directory TO {{app_role}};
REVOKE UPDATE, DELETE ON tenant_directory FROM {{app_role}};

ALTER TABLE tenant_versions ENABLE ROW LEVEL SECURITY;
ALTER TABLE tenant_versions FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON tenant_versions
  USING (tenant_id = app_tenant()) WITH CHECK (tenant_id = app_tenant());
