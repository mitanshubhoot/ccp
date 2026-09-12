-- Phase 0 schema: extensions, the tenant guard function, and the Layer 2 ingestion
-- tables (spec.md Layer 2). Later phases add files; nothing here is ever edited.

CREATE EXTENSION IF NOT EXISTS vector;

-- The one function every row-level security policy calls. It raises when the tenant
-- setting is missing or empty, so a query outside a ScopedDB transaction fails loudly
-- instead of returning empty tables (spec.md D15, A-39). current_setting(..., true)
-- alone is not enough: after any SET LOCAL in a pooled session the parameter exists
-- and reads as '' rather than raising.
CREATE OR REPLACE FUNCTION app_tenant() RETURNS text
LANGUAGE plpgsql STABLE AS $$
DECLARE
  t text;
BEGIN
  t := current_setting('app.tenant_id', true);
  IF t IS NULL OR t = '' THEN
    RAISE EXCEPTION 'app.tenant_id is not set; tenant data must be read through ScopedDB'
      USING ERRCODE = 'P0001';
  END IF;
  RETURN t;
END;
$$;

CREATE TABLE IF NOT EXISTS raw_events (
  tenant_id       TEXT NOT NULL,
  connector       TEXT NOT NULL,
  source_event_id TEXT NOT NULL,
  source_ts       TIMESTAMPTZ NOT NULL,
  received_at     TIMESTAMPTZ NOT NULL DEFAULT now(),   -- audit only, never used for ordering
  raw_path        TEXT NOT NULL,
  payload_sha256  TEXT NOT NULL,
  PRIMARY KEY (tenant_id, connector, source_event_id)
);

CREATE TABLE IF NOT EXISTS event_log (                  -- the Kafka stand-in
  "offset"        BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  tenant_id       TEXT NOT NULL,
  topic           TEXT NOT NULL,
  partition_key   TEXT NOT NULL,
  source_ts       TIMESTAMPTZ NOT NULL,
  ref             TEXT NOT NULL,
  produced_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
  -- parallel-topic claiming (unused on ordered topics)
  status          TEXT NOT NULL DEFAULT 'pending'
                  CHECK (status IN ('pending', 'claimed', 'done', 'dead', 'parked')),
  attempts        INT NOT NULL DEFAULT 0,
  claimed_at      TIMESTAMPTZ,
  last_error      TEXT
);
CREATE INDEX IF NOT EXISTS ix_event_log_topic ON event_log (topic, "offset");
CREATE INDEX IF NOT EXISTS ix_event_log_claim ON event_log (topic, status, claimed_at)
  WHERE status IN ('pending', 'claimed');

CREATE TABLE IF NOT EXISTS consumer_offsets (           -- ordered topics only; no tenant data
  consumer        TEXT NOT NULL,
  topic           TEXT NOT NULL,
  last_offset     BIGINT NOT NULL,
  PRIMARY KEY (consumer, topic)
);

CREATE TABLE IF NOT EXISTS dead_letter (
  "offset"        BIGINT PRIMARY KEY REFERENCES event_log ("offset"),
  tenant_id       TEXT NOT NULL,
  topic           TEXT NOT NULL,
  attempts        INT NOT NULL,
  last_error      TEXT NOT NULL,
  dead_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);
