-- Phase 3: canonical entity tables (spec.md Layer 3). Append-only observation rows; the
-- "current" view of an entity is its latest observation. Views are SECURITY INVOKER so
-- row-level security applies as the querying role, never as the view owner.

CREATE TABLE IF NOT EXISTS persons (
  tenant_id           TEXT NOT NULL,
  entity_id           TEXT NOT NULL,
  observed_at         TIMESTAMPTZ NOT NULL,
  source_connector    TEXT NOT NULL,
  source_event_id     TEXT NOT NULL,
  display_name        TEXT,                       -- NULL on partial observations
  emails              JSONB NOT NULL DEFAULT '[]',
  handles             JSONB NOT NULL DEFAULT '{}',
  employee_id         TEXT,
  account_kind        TEXT NOT NULL CHECK (account_kind IN ('human','service','shared','unknown')),
  employment_status   TEXT NOT NULL CHECK (employment_status IN ('active','terminated','unknown')),
  status_effective_at TIMESTAMPTZ,
  owner_employee_id   TEXT,
  PRIMARY KEY (tenant_id, entity_id, source_event_id)
);

CREATE TABLE IF NOT EXISTS access_grants (
  tenant_id           TEXT NOT NULL,
  entity_id           TEXT NOT NULL,
  observed_at         TIMESTAMPTZ NOT NULL,
  source_connector    TEXT NOT NULL,
  source_event_id     TEXT NOT NULL,
  person_entity_id    TEXT NOT NULL,
  asset_entity_id     TEXT NOT NULL,
  role                TEXT NOT NULL,
  state               TEXT NOT NULL CHECK (state IN ('active','revoked')),
  state_effective_at  TIMESTAMPTZ NOT NULL,
  attributes          JSONB NOT NULL DEFAULT '{}',
  PRIMARY KEY (tenant_id, entity_id, source_event_id)
);

CREATE TABLE IF NOT EXISTS assets (
  tenant_id           TEXT NOT NULL,
  entity_id           TEXT NOT NULL,
  observed_at         TIMESTAMPTZ NOT NULL,
  source_connector    TEXT NOT NULL,
  source_event_id     TEXT NOT NULL,
  name                TEXT NOT NULL,
  asset_type          TEXT NOT NULL,
  tags                JSONB NOT NULL DEFAULT '[]',
  settings            JSONB NOT NULL DEFAULT '{}',
  PRIMARY KEY (tenant_id, entity_id, source_event_id)
);

CREATE TABLE IF NOT EXISTS evidence (
  tenant_id           TEXT NOT NULL,
  entity_id           TEXT NOT NULL,
  observed_at         TIMESTAMPTZ NOT NULL,
  source_connector    TEXT NOT NULL,
  source_event_id     TEXT NOT NULL,
  evidence_type       TEXT NOT NULL,
  title               TEXT NOT NULL,
  text                TEXT NOT NULL,
  subject_entity_id   TEXT,
  issued_at           TIMESTAMPTZ,
  raw_ref             TEXT NOT NULL,
  page_texts          JSONB,
  guardrail_events    JSONB NOT NULL DEFAULT '[]',
  PRIMARY KEY (tenant_id, entity_id, source_event_id)
);
CREATE INDEX IF NOT EXISTS ix_evidence_type ON evidence (tenant_id, evidence_type, observed_at DESC);

CREATE TABLE IF NOT EXISTS vendors (
  tenant_id           TEXT NOT NULL,
  entity_id           TEXT NOT NULL,
  observed_at         TIMESTAMPTZ NOT NULL,
  source_connector    TEXT NOT NULL,
  source_event_id     TEXT NOT NULL,
  name                TEXT NOT NULL,
  vendor_type         TEXT NOT NULL,
  data_categories     JSONB NOT NULL DEFAULT '[]',
  PRIMARY KEY (tenant_id, entity_id, source_event_id)
);

CREATE TABLE IF NOT EXISTS findings (
  tenant_id           TEXT NOT NULL,
  entity_id           TEXT NOT NULL,
  observed_at         TIMESTAMPTZ NOT NULL,
  source_connector    TEXT NOT NULL,
  source_event_id     TEXT NOT NULL,
  test_id             TEXT NOT NULL,
  severity            TEXT NOT NULL,
  summary             TEXT NOT NULL,
  subject_entity_ids  JSONB NOT NULL DEFAULT '[]',
  PRIMARY KEY (tenant_id, entity_id, source_event_id)
);

-- Latest observation per entity. Asset settings are merged across observations by the
-- repository layer (a repo snapshot and its branch-protection snapshot are separate
-- events); every other current view is simply the newest row.
CREATE OR REPLACE VIEW current_persons WITH (security_invoker = true) AS
  SELECT DISTINCT ON (tenant_id, entity_id) *
  FROM persons ORDER BY tenant_id, entity_id, observed_at DESC, source_event_id DESC;

CREATE OR REPLACE VIEW current_access_grants WITH (security_invoker = true) AS
  SELECT DISTINCT ON (tenant_id, entity_id) *
  FROM access_grants ORDER BY tenant_id, entity_id, observed_at DESC, source_event_id DESC;

CREATE OR REPLACE VIEW current_assets WITH (security_invoker = true) AS
  SELECT DISTINCT ON (tenant_id, entity_id) *
  FROM assets ORDER BY tenant_id, entity_id, observed_at DESC, source_event_id DESC;

CREATE OR REPLACE VIEW current_evidence WITH (security_invoker = true) AS
  SELECT DISTINCT ON (tenant_id, entity_id) *
  FROM evidence ORDER BY tenant_id, entity_id, observed_at DESC, source_event_id DESC;

CREATE OR REPLACE VIEW current_vendors WITH (security_invoker = true) AS
  SELECT DISTINCT ON (tenant_id, entity_id) *
  FROM vendors ORDER BY tenant_id, entity_id, observed_at DESC, source_event_id DESC;

GRANT SELECT, INSERT ON persons, access_grants, assets, evidence, vendors, findings TO {{app_role}};
GRANT SELECT ON current_persons, current_access_grants, current_assets, current_evidence, current_vendors
  TO {{app_role}};

ALTER TABLE persons ENABLE ROW LEVEL SECURITY;
ALTER TABLE persons FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON persons
  USING (tenant_id = app_tenant()) WITH CHECK (tenant_id = app_tenant());

ALTER TABLE access_grants ENABLE ROW LEVEL SECURITY;
ALTER TABLE access_grants FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON access_grants
  USING (tenant_id = app_tenant()) WITH CHECK (tenant_id = app_tenant());

ALTER TABLE assets ENABLE ROW LEVEL SECURITY;
ALTER TABLE assets FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON assets
  USING (tenant_id = app_tenant()) WITH CHECK (tenant_id = app_tenant());

ALTER TABLE evidence ENABLE ROW LEVEL SECURITY;
ALTER TABLE evidence FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON evidence
  USING (tenant_id = app_tenant()) WITH CHECK (tenant_id = app_tenant());

ALTER TABLE vendors ENABLE ROW LEVEL SECURITY;
ALTER TABLE vendors FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON vendors
  USING (tenant_id = app_tenant()) WITH CHECK (tenant_id = app_tenant());

ALTER TABLE findings ENABLE ROW LEVEL SECURITY;
ALTER TABLE findings FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON findings
  USING (tenant_id = app_tenant()) WITH CHECK (tenant_id = app_tenant());
