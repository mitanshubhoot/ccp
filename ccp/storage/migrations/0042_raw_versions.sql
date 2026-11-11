-- MT5 step 1: every raw pointer says which bytes were stored, and what the provider sent
-- (spec.md A-136).
--   raw_events.stored_sha256   the digest of the bytes actually stored (payload_sha256 stays
--                              the de-duplication key: CCP's canonical digest of the payload)
--   raw_events.version_id      the object's version (S3), or the stored digest (local disk)
--   raw_events.store           local or s3
--   raw_responses              insert-only: each response a pull received, as bytes in the
--                              raw zone, with the provider's request id and the bytes' digest

ALTER TABLE raw_events
  ADD COLUMN IF NOT EXISTS stored_sha256 TEXT,
  ADD COLUMN IF NOT EXISTS version_id TEXT,
  ADD COLUMN IF NOT EXISTS store TEXT NOT NULL DEFAULT 'local' CHECK (store IN ('local', 's3'));

CREATE TABLE IF NOT EXISTS raw_responses (
  tenant_id     TEXT NOT NULL,
  response_id   TEXT NOT NULL,                       -- ULID
  connector     TEXT NOT NULL,
  sync_run_id   TEXT,
  method        TEXT NOT NULL,
  path          TEXT NOT NULL,
  status        INT NOT NULL,
  request_id    TEXT,                                -- the provider's, e.g. X-GitHub-Request-Id
  body_sha256   TEXT NOT NULL,
  ref           TEXT NOT NULL,
  version_id    TEXT NOT NULL,
  store         TEXT NOT NULL CHECK (store IN ('local', 's3')),
  received_at   TIMESTAMPTZ NOT NULL,
  PRIMARY KEY (tenant_id, response_id)
);
CREATE INDEX IF NOT EXISTS ix_raw_responses_run ON raw_responses (tenant_id, sync_run_id);

GRANT SELECT, INSERT ON raw_responses TO {{app_role}};
REVOKE UPDATE, DELETE ON raw_responses FROM {{app_role}};
ALTER TABLE raw_responses ENABLE ROW LEVEL SECURITY;
ALTER TABLE raw_responses FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON raw_responses
  USING (tenant_id = app_tenant()) WITH CHECK (tenant_id = app_tenant());
