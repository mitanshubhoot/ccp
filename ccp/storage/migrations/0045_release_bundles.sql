-- MT7 step 1: every AI result names the release bundle it ran under (spec.md A-142).
--   release_bundles         each bundle's manifest, once: models and their behaviour, the
--                           prompt and tool hash, the rule catalogue, configuration digests.
--                           Not tenant data: the same bundle serves every tenant
--   verdicts.bundle_id      the bundle a verdict was produced under
--   agent_threads.bundle_id the bundle an agent run started under

CREATE TABLE IF NOT EXISTS release_bundles (
  bundle_id      TEXT PRIMARY KEY,
  manifest       JSONB NOT NULL,
  first_seen_at  TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);

ALTER TABLE verdicts ADD COLUMN IF NOT EXISTS bundle_id TEXT;
ALTER TABLE agent_threads ADD COLUMN IF NOT EXISTS bundle_id TEXT;
CREATE INDEX IF NOT EXISTS ix_verdicts_bundle ON verdicts (tenant_id, bundle_id);

GRANT SELECT, INSERT ON release_bundles TO {{app_role}};
REVOKE UPDATE, DELETE ON release_bundles FROM {{app_role}};
