-- MT5 step 2: a per-tenant hash chain over the record, and signed checkpoints of it
-- (spec.md A-137).
--   audit_chain          one entry per row inserted into a chained table: the row's digest,
--                        its key and columns, and entry = sha256(prev | seq | table | digest);
--                        written by a deferred trigger at the inserting transaction's commit,
--                        so the head lock is the last lock it takes and entries follow commit
--                        order. Only that trigger writes here (it runs as the owner); the
--                        application may read its own tenant's entries
--   audit_chain_heads    the latest entry per tenant; the owner's alone
--   audit_checkpoints    insert-only: a signed statement of a tenant's head at a moment,
--                        also exported outside the database (ccp/audit/checkpoints.py)

CREATE TABLE IF NOT EXISTS audit_chain (
  tenant_id     TEXT NOT NULL,
  seq           BIGINT NOT NULL,
  table_name    TEXT NOT NULL,
  row_key       JSONB NOT NULL,
  row_columns   TEXT[] NOT NULL,
  row_sha256    TEXT NOT NULL,
  prev_sha256   TEXT NOT NULL,
  entry_sha256  TEXT NOT NULL,
  recorded_at   TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  PRIMARY KEY (tenant_id, seq)
);

CREATE TABLE IF NOT EXISTS audit_chain_heads (
  tenant_id     TEXT PRIMARY KEY,
  seq           BIGINT NOT NULL,
  entry_sha256  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_checkpoints (
  tenant_id     TEXT NOT NULL,
  seq           BIGINT NOT NULL,
  entry_sha256  TEXT NOT NULL,
  statement     JSONB NOT NULL,                      -- exactly what was signed
  signature     TEXT NOT NULL,                       -- base64
  key_id        TEXT NOT NULL,
  exported_to   TEXT NOT NULL,
  signed_at     TIMESTAMPTZ NOT NULL,
  PRIMARY KEY (tenant_id, seq, key_id)
);

-- the digest of a row as JSON, in UTC, so it reads the same in every session
CREATE OR REPLACE FUNCTION audit_row_sha256(row_json JSONB) RETURNS TEXT
  LANGUAGE sql IMMUTABLE AS $$
    SELECT encode(sha256(convert_to(row_json::text, 'UTF8')), 'hex')
  $$;

-- one entry for one row; the caller holds no other lock it could wait on (a deferred trigger)
CREATE OR REPLACE FUNCTION audit_chain_record(tenant TEXT, tbl TEXT, rel REGCLASS, row_json JSONB)
  RETURNS VOID
  LANGUAGE plpgsql SECURITY DEFINER
  SET search_path = public, pg_temp
  SET timezone = 'UTC'
AS $$
DECLARE
  key_json JSONB;
  digest TEXT;
  head RECORD;
  next_seq BIGINT;
  entry TEXT;
BEGIN
  SELECT jsonb_object_agg(a.attname, row_json -> a.attname) INTO key_json
  FROM pg_index i
  JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = ANY (i.indkey)
  WHERE i.indrelid = rel AND i.indisprimary;
  digest := audit_row_sha256(row_json);
  INSERT INTO audit_chain_heads (tenant_id, seq, entry_sha256)
    VALUES (tenant, 0, repeat('0', 64)) ON CONFLICT (tenant_id) DO NOTHING;
  SELECT h.seq, h.entry_sha256 INTO head
    FROM audit_chain_heads h WHERE h.tenant_id = tenant FOR UPDATE;
  next_seq := head.seq + 1;
  entry := encode(sha256(convert_to(
    head.entry_sha256 || '|' || next_seq::text || '|' || tbl || '|' || digest, 'UTF8')), 'hex');
  INSERT INTO audit_chain (tenant_id, seq, table_name, row_key, row_columns, row_sha256,
                           prev_sha256, entry_sha256)
    VALUES (tenant, next_seq, tbl, coalesce(key_json, row_json),
            ARRAY(SELECT jsonb_object_keys(row_json)), digest, head.entry_sha256, entry);
  UPDATE audit_chain_heads SET seq = next_seq, entry_sha256 = entry WHERE tenant_id = tenant;
END
$$;

CREATE OR REPLACE FUNCTION audit_chain_append() RETURNS trigger
  LANGUAGE plpgsql SECURITY DEFINER
  SET search_path = public, pg_temp
  SET timezone = 'UTC'
AS $$
BEGIN
  PERFORM audit_chain_record(NEW.tenant_id, TG_TABLE_NAME, TG_RELID, to_jsonb(NEW));
  RETURN NULL;
END
$$;

-- the record: what CCP observed, believed, decided and did
DO $$
DECLARE
  t TEXT;
  r RECORD;
BEGIN
  PERFORM set_config('timezone', 'UTC', true);
  FOREACH t IN ARRAY ARRAY[
    'raw_events', 'raw_responses', 'verdicts', 'compliance_state', 'rule_changes',
    'approvals', 'approval_events', 'tenant_members', 'tenant_versions',
    'remediation_proposals', 'proposal_events', 'execution_grants', 'grant_uses',
    'execution_journal', 'credential_issues', 'control_exceptions', 'exception_revocations',
    'review_resolutions', 'person_links', 'run_stops'
  ] LOOP
    -- genesis: the rows already there open each tenant's chain, in a fixed order
    FOR r IN EXECUTE format(
      'SELECT tenant_id, to_jsonb(x) AS j FROM %I x ORDER BY to_jsonb(x)::text', t) LOOP
      PERFORM audit_chain_record(r.tenant_id, t, t::regclass, r.j);
    END LOOP;
    EXECUTE format('DROP TRIGGER IF EXISTS audit_chain ON %I', t);
    EXECUTE format(
      'CREATE CONSTRAINT TRIGGER audit_chain AFTER INSERT ON %I'
      ' DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION audit_chain_append()', t);
  END LOOP;
END
$$;

REVOKE ALL ON audit_chain, audit_chain_heads FROM {{app_role}};
GRANT SELECT ON audit_chain TO {{app_role}};
GRANT SELECT, INSERT ON audit_checkpoints TO {{app_role}};
REVOKE UPDATE, DELETE ON audit_checkpoints FROM {{app_role}};

ALTER TABLE audit_chain ENABLE ROW LEVEL SECURITY;
ALTER TABLE audit_chain FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON audit_chain
  USING (tenant_id = app_tenant()) WITH CHECK (tenant_id = app_tenant());
ALTER TABLE audit_chain_heads ENABLE ROW LEVEL SECURITY;
ALTER TABLE audit_chain_heads FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON audit_chain_heads
  USING (tenant_id = app_tenant()) WITH CHECK (tenant_id = app_tenant());
ALTER TABLE audit_checkpoints ENABLE ROW LEVEL SECURITY;
ALTER TABLE audit_checkpoints FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON audit_checkpoints
  USING (tenant_id = app_tenant()) WITH CHECK (tenant_id = app_tenant());
