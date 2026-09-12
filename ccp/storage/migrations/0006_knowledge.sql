-- Phase 6: the knowledge graph as reference data (spec.md Layer 6). These tables hold no
-- tenant data, so they carry no tenant_id and no row-level security, and the loader may
-- upsert them (the one place a plain UPDATE is allowed).

CREATE TABLE IF NOT EXISTS frameworks (
  framework_id  TEXT PRIMARY KEY,
  name          TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS requirements (
  framework_id    TEXT NOT NULL REFERENCES frameworks (framework_id),
  requirement_id  TEXT NOT NULL,
  title           TEXT NOT NULL,
  intent          TEXT NOT NULL,
  clauses         TEXT[] NOT NULL,
  PRIMARY KEY (framework_id, requirement_id)
);

CREATE TABLE IF NOT EXISTS controls (
  control_id    TEXT PRIMARY KEY,
  title         TEXT NOT NULL,
  description   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tests (
  test_id             TEXT PRIMARY KEY,
  title               TEXT NOT NULL,
  evaluation          TEXT NOT NULL CHECK (evaluation IN ('deterministic','interpretive')),
  evidence_types      TEXT[] NOT NULL,
  subject_kind        TEXT NOT NULL,
  rule                TEXT NOT NULL,
  valid_from_strategy TEXT NOT NULL,
  tier                SMALLINT NOT NULL CHECK (tier IN (1, 2, 3)),
  spec                JSONB NOT NULL
);

CREATE TABLE IF NOT EXISTS requirement_controls (
  framework_id    TEXT NOT NULL,
  requirement_id  TEXT NOT NULL,
  control_id      TEXT NOT NULL REFERENCES controls (control_id),
  PRIMARY KEY (framework_id, requirement_id, control_id),
  FOREIGN KEY (framework_id, requirement_id) REFERENCES requirements (framework_id, requirement_id)
);

CREATE TABLE IF NOT EXISTS control_tests (
  control_id  TEXT NOT NULL REFERENCES controls (control_id),
  test_id     TEXT NOT NULL REFERENCES tests (test_id),
  PRIMARY KEY (control_id, test_id)
);

GRANT SELECT, INSERT, UPDATE ON frameworks, requirements, controls, tests, requirement_controls, control_tests
  TO {{app_role}};
