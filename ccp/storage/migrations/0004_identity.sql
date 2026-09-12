-- Phase 4: identity resolution (spec.md Layer 4). Everything append-only; "current" is
-- derived. A review item is open until a resolution row references it.

CREATE TABLE IF NOT EXISTS resolution_decisions (
  decision_id      TEXT PRIMARY KEY,
  tenant_id        TEXT NOT NULL,
  left_entity_id   TEXT NOT NULL,
  right_entity_id  TEXT NOT NULL,
  block_keys       JSONB NOT NULL,        -- which block keys produced the pair
  gate_result      TEXT NOT NULL CHECK (gate_result IN ('passed','blocked_kind_mismatch','blocked_shared_account')),
  features         JSONB NOT NULL,        -- every feature name -> value compared
  score            DOUBLE PRECISION,      -- NULL when gated
  route            TEXT NOT NULL CHECK (route IN ('auto_merge','auto_distinct','review','blocked')),
  thresholds       JSONB NOT NULL,        -- the weights and thresholds in force
  recorded_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_decisions_pair ON resolution_decisions (tenant_id, left_entity_id, right_entity_id, recorded_at DESC);

CREATE TABLE IF NOT EXISTS person_links (
  link_id             TEXT PRIMARY KEY,
  tenant_id           TEXT NOT NULL,
  person_entity_id    TEXT NOT NULL,
  canonical_person_id TEXT NOT NULL,
  decided_by          TEXT NOT NULL CHECK (decided_by IN ('auto_merge','human')),
  decision_id         TEXT NOT NULL REFERENCES resolution_decisions (decision_id),
  recorded_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_links_person ON person_links (tenant_id, person_entity_id, recorded_at DESC);

CREATE TABLE IF NOT EXISTS review_queue (
  decision_id      TEXT PRIMARY KEY REFERENCES resolution_decisions (decision_id),
  tenant_id        TEXT NOT NULL,
  opened_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS review_resolutions (
  resolution_id    TEXT PRIMARY KEY,
  decision_id      TEXT NOT NULL REFERENCES review_queue (decision_id),
  tenant_id        TEXT NOT NULL,
  outcome          TEXT NOT NULL CHECK (outcome IN ('merge','distinct')),
  reviewer         TEXT NOT NULL,
  note             TEXT,
  resolved_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE OR REPLACE VIEW current_person_links WITH (security_invoker = true) AS
  SELECT DISTINCT ON (tenant_id, person_entity_id) *
  FROM person_links ORDER BY tenant_id, person_entity_id, recorded_at DESC, link_id DESC;

CREATE OR REPLACE VIEW open_review_queue WITH (security_invoker = true) AS
  SELECT q.* FROM review_queue q
  WHERE NOT EXISTS (SELECT 1 FROM review_resolutions r WHERE r.decision_id = q.decision_id);

GRANT SELECT, INSERT ON resolution_decisions, person_links, review_queue, review_resolutions TO {{app_role}};
GRANT SELECT ON current_person_links, open_review_queue TO {{app_role}};

ALTER TABLE resolution_decisions ENABLE ROW LEVEL SECURITY;
ALTER TABLE resolution_decisions FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON resolution_decisions
  USING (tenant_id = app_tenant()) WITH CHECK (tenant_id = app_tenant());

ALTER TABLE person_links ENABLE ROW LEVEL SECURITY;
ALTER TABLE person_links FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON person_links
  USING (tenant_id = app_tenant()) WITH CHECK (tenant_id = app_tenant());

ALTER TABLE review_queue ENABLE ROW LEVEL SECURITY;
ALTER TABLE review_queue FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON review_queue
  USING (tenant_id = app_tenant()) WITH CHECK (tenant_id = app_tenant());

ALTER TABLE review_resolutions ENABLE ROW LEVEL SECURITY;
ALTER TABLE review_resolutions FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON review_resolutions
  USING (tenant_id = app_tenant()) WITH CHECK (tenant_id = app_tenant());
