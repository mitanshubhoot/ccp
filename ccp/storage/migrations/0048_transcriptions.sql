-- MT9 step 3: Claude transcription of pages with no text layer and of screenshots, with the
-- method in provenance (spec.md A-147).
--
--   evidence.page_methods   how each page's text was obtained, one entry per page (or one for a
--                           single text): 'text_layer', 'markdown', 'transcribed:<model>:<prompt>',
--                           'blank:awaiting_transcription', 'illegible:<model>:<prompt>'
--   transcriptions          insert-only: one row per transcribed page or image: the upload it
--                           came from, the digest of the exact bytes sent (kept in the raw zone),
--                           the model and prompt version, whether it was legible, and the cost

ALTER TABLE evidence ADD COLUMN IF NOT EXISTS page_methods JSONB;

CREATE TABLE IF NOT EXISTS transcriptions (
  tenant_id         TEXT NOT NULL,
  transcription_id  TEXT NOT NULL,
  source_event_id   TEXT NOT NULL,            -- the upload's raw event
  filename          TEXT NOT NULL,
  page              INT,                      -- NULL: an image
  input_sha256      TEXT NOT NULL,            -- the page or image bytes sent to the model
  model             TEXT NOT NULL,
  prompt_version    TEXT NOT NULL,
  legible           BOOLEAN NOT NULL,
  text_sha256       TEXT NOT NULL,
  chars             INT NOT NULL,
  usd               NUMERIC(12, 6) NOT NULL DEFAULT 0,
  recorded_at       TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  PRIMARY KEY (tenant_id, transcription_id)
);
CREATE INDEX IF NOT EXISTS ix_transcriptions_source ON transcriptions (tenant_id, source_event_id);

GRANT SELECT, INSERT ON transcriptions TO {{app_role}};
REVOKE UPDATE, DELETE ON transcriptions FROM {{app_role}};
ALTER TABLE transcriptions ENABLE ROW LEVEL SECURITY;
ALTER TABLE transcriptions FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON transcriptions
  USING (tenant_id = app_tenant()) WITH CHECK (tenant_id = app_tenant());
