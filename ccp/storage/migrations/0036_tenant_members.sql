-- MT4 step 1: roles per tenant (spec.md A-129).
--   tenant_members   one row per version of a membership: the person's issuer and subject
--                    (stable at their identity provider), an email and name for display,
--                    the role, whether it is active, and who granted it. A change of role
--                    or a revocation is a new version, so the history says who could
--                    approve what, when, and on whose grant.

CREATE TABLE IF NOT EXISTS tenant_members (
  tenant_id    TEXT NOT NULL,
  member_id    TEXT NOT NULL,                       -- ULID of this version
  issuer       TEXT NOT NULL,
  subject      TEXT NOT NULL,
  email        TEXT,
  name         TEXT,
  role         TEXT NOT NULL CHECK (role IN ('viewer', 'reviewer', 'approver', 'admin')),
  active       BOOLEAN NOT NULL,
  granted_by   TEXT NOT NULL CHECK (granted_by <> ''),
  recorded_at  TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  PRIMARY KEY (tenant_id, member_id)
);
CREATE INDEX IF NOT EXISTS ix_tenant_members_person
  ON tenant_members (tenant_id, issuer, subject, recorded_at);

CREATE OR REPLACE VIEW current_tenant_members WITH (security_invoker = true) AS
  SELECT DISTINCT ON (tenant_id, issuer, subject) *
  FROM tenant_members ORDER BY tenant_id, issuer, subject, recorded_at DESC, member_id DESC;

GRANT SELECT, INSERT ON tenant_members TO {{app_role}};
REVOKE UPDATE, DELETE ON tenant_members FROM {{app_role}};
GRANT SELECT ON current_tenant_members TO {{app_role}};
ALTER TABLE tenant_members ENABLE ROW LEVEL SECURITY;
ALTER TABLE tenant_members FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON tenant_members
  USING (tenant_id = app_tenant()) WITH CHECK (tenant_id = app_tenant());
