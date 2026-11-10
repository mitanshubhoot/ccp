-- MT3: every credential the remediation broker issues, appended (spec.md A-116).
--   credential_issues  one row per credential: the operation (a proposal) and action it is
--                      for, the approval item behind it and that item's payload hash, the
--                      scope it may touch, and when it was issued and runs out. The broker
--                      issues nothing without an approved item for that operation. Never
--                      edited.

CREATE TABLE IF NOT EXISTS credential_issues (
  issue_id        TEXT PRIMARY KEY,                  -- ULID
  tenant_id       TEXT NOT NULL,
  operation       TEXT NOT NULL,                     -- the proposal
  action          TEXT NOT NULL CHECK (action IN ('execute', 'rollback')),
  approval_id     TEXT NOT NULL,
  payload_sha256  TEXT NOT NULL,
  scope           TEXT NOT NULL,
  issued_at       TIMESTAMPTZ NOT NULL,
  expires_at      TIMESTAMPTZ NOT NULL,
  CHECK (expires_at > issued_at)
);
CREATE INDEX IF NOT EXISTS ix_credential_issues_operation
  ON credential_issues (tenant_id, operation, issued_at);

GRANT SELECT, INSERT ON credential_issues TO {{app_role}};
REVOKE UPDATE, DELETE ON credential_issues FROM {{app_role}};
ALTER TABLE credential_issues ENABLE ROW LEVEL SECURITY;
ALTER TABLE credential_issues FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON credential_issues
  USING (tenant_id = app_tenant()) WITH CHECK (tenant_id = app_tenant());
