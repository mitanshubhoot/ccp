-- MT6 step 1: AWS accounts onboard to a read-only role CCP assumes with its own external ID
-- (spec.md A-139); the identity-provider connector follows (A-141).
--   connections.connector  aws and idp join github, hris and manual
--   aws_onboardings        insert-only versions per account: the external ID CCP generated,
--                          the role the customer created, and each verification's outcome
--                          and findings (pending, verified, refused)

ALTER TABLE connections DROP CONSTRAINT IF EXISTS connections_connector_check;
ALTER TABLE connections ADD CONSTRAINT connections_connector_check
  CHECK (connector IN ('github', 'hris', 'manual', 'aws', 'idp'));

CREATE TABLE IF NOT EXISTS aws_onboardings (
  tenant_id      TEXT NOT NULL,
  onboarding_id  TEXT NOT NULL,                       -- ULID
  account_id     TEXT NOT NULL CHECK (account_id ~ '^[0-9]{12}$'),
  external_id    TEXT NOT NULL CHECK (length(external_id) >= 32),
  role_arn       TEXT,
  status         TEXT NOT NULL CHECK (status IN ('pending', 'verified', 'refused')),
  findings       JSONB NOT NULL DEFAULT '[]'::jsonb,
  recorded_by    TEXT NOT NULL CHECK (recorded_by <> ''),
  recorded_at    TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  PRIMARY KEY (tenant_id, onboarding_id)
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_aws_onboardings_external_id
  ON aws_onboardings (external_id, onboarding_id);

CREATE OR REPLACE VIEW current_aws_onboardings WITH (security_invoker = true) AS
  SELECT DISTINCT ON (tenant_id, account_id) *
  FROM aws_onboardings ORDER BY tenant_id, account_id, recorded_at DESC, onboarding_id DESC;

GRANT SELECT, INSERT ON aws_onboardings TO {{app_role}};
REVOKE UPDATE, DELETE ON aws_onboardings FROM {{app_role}};
GRANT SELECT ON current_aws_onboardings TO {{app_role}};
ALTER TABLE aws_onboardings ENABLE ROW LEVEL SECURITY;
ALTER TABLE aws_onboardings FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON aws_onboardings
  USING (tenant_id = app_tenant()) WITH CHECK (tenant_id = app_tenant());
