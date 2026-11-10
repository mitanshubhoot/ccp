-- MT2: fair dispatch (spec.md A-106).
--   work_hints  one row per (tenant, consumer): whether the consumer may have work for the
--               tenant (ready_at; NULL is idle) and when a worker last served it (served_at).
--               A worker claims the ready hint served longest ago, serves one quantum and
--               puts it back, so one tenant's backlog cannot starve another. It holds ids and
--               times only, no tenant data, and workers claim across tenants, so it carries
--               no row-level security; the catalogue lint pins its columns (A-99).

CREATE TABLE IF NOT EXISTS work_hints (
  tenant_id   TEXT NOT NULL,
  consumer    TEXT NOT NULL,
  ready_at    TIMESTAMPTZ,
  served_at   TIMESTAMPTZ,
  PRIMARY KEY (tenant_id, consumer)
);
CREATE INDEX IF NOT EXISTS ix_work_hints_ready ON work_hints (consumer, ready_at);
GRANT SELECT, INSERT, UPDATE ON work_hints TO {{app_role}};   -- dispatch metadata
