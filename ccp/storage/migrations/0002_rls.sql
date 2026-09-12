-- Grants and row-level security for the Phase 0 tables (spec.md Layer 5, A-39).
-- {{app_role}} is substituted by the migration runner from DATABASE_URL.
-- The app role is NOBYPASSRLS and does not own the tables, so FORCE applies to it.

GRANT USAGE ON SCHEMA public TO {{app_role}};
GRANT EXECUTE ON FUNCTION app_tenant() TO {{app_role}};
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {{app_role}};

-- Append-only tables: SELECT and INSERT only. No UPDATE or DELETE is ever granted.
GRANT SELECT, INSERT ON raw_events, dead_letter TO {{app_role}};
-- Operational tables: parallel consumers update status and attempts; ordered
-- consumers advance their cursor.
GRANT SELECT, INSERT, UPDATE ON event_log, consumer_offsets TO {{app_role}};

ALTER TABLE raw_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE raw_events FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON raw_events
  USING (tenant_id = app_tenant())
  WITH CHECK (tenant_id = app_tenant());

ALTER TABLE event_log ENABLE ROW LEVEL SECURITY;
ALTER TABLE event_log FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON event_log
  USING (tenant_id = app_tenant())
  WITH CHECK (tenant_id = app_tenant());

ALTER TABLE dead_letter ENABLE ROW LEVEL SECURITY;
ALTER TABLE dead_letter FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON dead_letter
  USING (tenant_id = app_tenant())
  WITH CHECK (tenant_id = app_tenant());
