-- MT3: a read-only database role for agent tools (spec.md A-115).
-- {{agent_role}} is created by the migration runner from DATABASE_AGENT_URL: LOGIN,
-- NOSUPERUSER, NOBYPASSRLS, and not the owner of anything, so row-level security applies
-- to it as it does to the application role. It may read only what the tools read, through
-- the same tenant setting, and holds no INSERT, UPDATE or DELETE anywhere: a tool bug, or a
-- tool driven by a model, cannot write. The catalogue lint checks that it stays so.

GRANT USAGE ON SCHEMA public TO {{agent_role}};
GRANT EXECUTE ON FUNCTION app_tenant() TO {{agent_role}};
GRANT SELECT ON
  chunks,
  persons, access_grants, assets, evidence, vendors,
  current_persons, current_access_grants, current_assets, current_evidence, current_vendors,
  person_links, resolution_decisions, current_person_links,
  compliance_state, verdicts, investigations
TO {{agent_role}};
