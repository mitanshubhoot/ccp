"""The row-level security catalogue lint (spec.md A-99), run against a migrated database.

For every table that holds tenant data (a `tenant_id` column in `public`, or the saver's
`thread_id` tables in `checkpoints`) it checks that
  1. row-level security is enabled and forced, so even the owner is filtered;
  2. there is exactly one policy, `tenant_isolation`, filtering both reads and writes on the
     tenant (another permissive policy would be OR-ed with it and open the table);
  3. an insert-only table grants the application role no UPDATE or DELETE;
  4. the table is classified: insert-only or operational. A new table that is neither fails
     the lint until someone decides which it is.
A deliberately permissive policy (`USING (true)`) is its negative control in the tests.

Dispatch tables (A-106) are the one exception: workers read them across tenants, so they
carry no row-level security, and the lint instead pins their columns to ids and times, so
no tenant data can be added to them unnoticed.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import psycopg

TENANT_PREDICATE = "(tenant_id = app_tenant())"
THREAD_PREDICATE = "(split_part(thread_id, ':'::text, 1) = app_tenant())"

# Audit and append-only data: corrections are new rows, never edits.
INSERT_ONLY: frozenset[str] = frozenset(
    {
        "raw_events",
        "dead_letter",
        "dead_letter_replays",
        "persons",
        "access_grants",
        "assets",
        "evidence",
        "vendors",
        "findings",
        "resolution_decisions",
        "person_links",
        "review_queue",
        "review_resolutions",
        "verdicts",
        "compliance_state",
        "spend_ledger",
        "embedding_cache",
        "enrichment_cache",
        "approvals",
        "approval_events",
        "investigations",
        "gap_reports",
        "policy_drafts",
        "copilot_answers",
        "review_labels",
        "schedule_runs",
        "agent_steps",
        "policy_decisions",
        "credential_issues",
        "sync_runs",
        "sync_run_scopes",
        "connections",
        "connection_checks",
        "subject_inventories",
        "replay_checks",
        "rule_changes",
        "tenant_members",
        "remediation_proposals",
        "proposal_events",
        "execution_grants",
        "grant_uses",
        "execution_journal",
        "control_exceptions",
        "exception_revocations",
        "tenant_versions",
        "run_stops",
        "raw_responses",
        "audit_chain",
        "audit_checkpoints",
        "aws_onboardings",
        "model_refusals",
        "quality_checks",
    }
)
# Operational state that is updated in place by design.
OPERATIONAL: frozenset[str] = frozenset(
    {
        "event_log",
        "tenant_sequences",
        "chunks",
        "agent_threads",
        "audit_chain_heads",
        "ordered_failures",
        "schedules",
        "checkpoints.checkpoints",
        "checkpoints.checkpoint_blobs",
        "checkpoints.checkpoint_writes",
    }
)
# Cross-tenant dispatch metadata (A-106): no row-level security, and exactly these columns.
DISPATCH: dict[str, frozenset[str]] = {
    "work_hints": frozenset({"tenant_id", "consumer", "ready_at", "served_at"}),
    "tenant_directory": frozenset({"tenant_id", "created_at"}),  # which tenants exist (A-133)
}


@dataclass(frozen=True)
class Finding:
    table: str
    problem: str

    def __str__(self) -> str:
        return f"{self.table}: {self.problem}"


def _tenant_tables(cur: psycopg.Cursor[Any]) -> list[tuple[str, str, bool, bool]]:
    """(qualified name, the column it is scoped by, rls enabled, rls forced)."""
    rows = cur.execute(
        "SELECT CASE WHEN n.nspname = 'public' THEN c.relname"
        "            ELSE n.nspname || '.' || c.relname END,"
        "       a.attname, c.relrowsecurity, c.relforcerowsecurity"
        " FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace"
        " JOIN pg_attribute a ON a.attrelid = c.oid AND NOT a.attisdropped"
        " WHERE c.relkind IN ('r', 'p')"
        "   AND ((n.nspname = 'public' AND a.attname = 'tenant_id')"
        "     OR (n.nspname = 'checkpoints' AND a.attname = 'thread_id'))"
        " ORDER BY 1"
    ).fetchall()
    return [(str(r[0]), str(r[1]), bool(r[2]), bool(r[3])) for r in rows]


def _policies(cur: psycopg.Cursor[Any], table: str) -> list[tuple[str, str, str, str | None]]:
    schema, _, name = table.rpartition(".")
    rows = cur.execute(
        "SELECT policyname, permissive, coalesce(qual, ''), with_check FROM pg_policies"
        " WHERE schemaname = %s AND tablename = %s",
        (schema or "public", name),
    ).fetchall()
    return [(str(r[0]), str(r[1]), str(r[2]), r[3]) for r in rows]


def _columns(cur: psycopg.Cursor[Any], table: str) -> set[str]:
    rows = cur.execute(
        "SELECT column_name FROM information_schema.columns"
        " WHERE table_schema = 'public' AND table_name = %s",
        (table,),
    ).fetchall()
    return {str(r[0]) for r in rows}


def _agent_role(cur: psycopg.Cursor[Any], agent_role: str) -> list[Finding]:
    """The agent tools' role (A-115) reads and nothing more, and row security binds it."""
    findings: list[Finding] = []
    role = cur.execute(
        "SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = %s", (agent_role,)
    ).fetchone()
    if role is None:
        return [Finding(agent_role, "the agent role does not exist")]
    if role[0] or role[1]:
        findings.append(Finding(agent_role, "the agent role is a superuser or bypasses RLS"))
    rows = cur.execute(
        "SELECT table_schema || '.' || table_name, privilege_type"
        " FROM information_schema.role_table_grants WHERE grantee = %s"
        " AND privilege_type IN ('INSERT', 'UPDATE', 'DELETE', 'TRUNCATE')"
        " ORDER BY 1, 2",
        (agent_role,),
    ).fetchall()
    findings.extend(Finding(str(t), f"the agent role holds {p}") for t, p in rows)
    return findings


def lint(
    conn: psycopg.Connection[Any], app_role: str, agent_role: str | None = None
) -> list[Finding]:
    findings: list[Finding] = []
    with conn.cursor() as cur:
        if agent_role is not None:
            findings.extend(_agent_role(cur, agent_role))
        for table, column, enabled, forced in _tenant_tables(cur):
            if table in DISPATCH:
                extra = _columns(cur, table) ^ DISPATCH[table]
                if extra:
                    findings.append(
                        Finding(
                            table,
                            f"dispatch table columns differ from {sorted(DISPATCH[table])}:"
                            f" {sorted(extra)}",
                        )
                    )
                continue
            if not (enabled and forced):
                findings.append(Finding(table, "row-level security is not enabled and forced"))
            predicate = TENANT_PREDICATE if column == "tenant_id" else THREAD_PREDICATE
            policies = _policies(cur, table)
            if [p[0] for p in policies] != ["tenant_isolation"]:
                findings.append(
                    Finding(
                        table,
                        f"policies {sorted(p[0] for p in policies)} are not exactly"
                        " ['tenant_isolation']",
                    )
                )
            for name, permissive, qual, check in policies:
                if name != "tenant_isolation":
                    continue
                if permissive != "PERMISSIVE" or qual != predicate or check != predicate:
                    findings.append(
                        Finding(
                            table,
                            f"tenant_isolation filters {qual!r} / {check!r}, not"
                            f" {predicate!r} on both reads and writes",
                        )
                    )
            if table in INSERT_ONLY:
                for privilege in ("UPDATE", "DELETE"):
                    row = cur.execute(
                        "SELECT has_table_privilege(%s, %s, %s)", (app_role, table, privilege)
                    ).fetchone()
                    if row is not None and row[0]:
                        findings.append(Finding(table, f"insert-only, yet {privilege} is granted"))
            elif table not in OPERATIONAL:
                findings.append(
                    Finding(table, "unclassified: add it to INSERT_ONLY or OPERATIONAL")
                )
    return findings
