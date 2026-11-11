"""`ccp audit verify` (spec.md A-137): the record checked against itself.

1. The chain: entries numbered from 1 with no gap, each linking to the one before, each
   equal to sha256(prev | seq | table | row digest).
2. The rows: every chained row re-hashed in the database, over the columns it had when it
   was chained, in UTC; a row gone is `missing`, a row whose digest changed is `altered`, and
   a row in a chained table with no entry (written with the trigger off) is `unchained`.
3. The checkpoints: each signature verifies over its statement, the statement names the
   chain's own entry at that seq, and the exported copy outside the database says the same.

Zero findings is the only clean result; the restore drill (tests) alters one row and expects
exactly one finding.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

import psycopg

from ccp.audit import checkpoints
from ccp.audit.signing import Signer, canonical, signer_for, unb64
from ccp.config import Settings
from ccp.storage.scoped import ScopedDB

GENESIS = "0" * 64
CHAINED = (
    "raw_events",
    "raw_responses",
    "verdicts",
    "compliance_state",
    "rule_changes",
    "approvals",
    "approval_events",
    "tenant_members",
    "tenant_versions",
    "remediation_proposals",
    "proposal_events",
    "execution_grants",
    "grant_uses",
    "execution_journal",
    "credential_issues",
    "control_exceptions",
    "exception_revocations",
    "review_resolutions",
    "person_links",
    "run_stops",
)


@dataclass
class AuditReport:
    entries: int = 0
    rows: int = 0
    checkpoints: int = 0
    findings: list[str] = field(default_factory=list)

    @property
    def clean(self) -> bool:
        return not self.findings


def _chain(db: ScopedDB, report: AuditReport) -> dict[int, str]:
    rows = db.fetch_all(
        "SELECT seq, table_name, row_sha256, prev_sha256, entry_sha256 FROM audit_chain"
        " ORDER BY seq"
    )
    by_seq: dict[int, str] = {}
    prev, expected_seq = GENESIS, 1
    for seq, table, digest, linked, entry in rows:
        seq = int(seq)
        if seq != expected_seq:
            report.findings.append(f"chain: entry {expected_seq} is missing (next is {seq})")
        if linked != prev:
            report.findings.append(f"chain: entry {seq} does not link to the entry before it")
        material = f"{linked}|{seq}|{table}|{digest}".encode()
        if hashlib.sha256(material).hexdigest() != entry:
            report.findings.append(f"chain: entry {seq} does not hash to what it records")
        by_seq[seq] = str(entry)
        prev, expected_seq = str(entry), seq + 1
    report.entries = len(rows)
    return by_seq


def _key_columns(cur: psycopg.Cursor[Any], table: str) -> list[str]:
    rows = cur.execute(
        "SELECT a.attname FROM pg_index i JOIN pg_attribute a"
        " ON a.attrelid = i.indrelid AND a.attnum = ANY (i.indkey)"
        " WHERE i.indrelid = %s::regclass AND i.indisprimary ORDER BY a.attname",
        (table,),
    ).fetchall()
    return [str(r[0]) for r in rows]


def _rows(db: ScopedDB, report: AuditReport) -> None:
    with db.transaction() as cur:
        cur.execute("SET LOCAL TimeZone = 'UTC'")  # as the trigger hashed them
        for table in CHAINED:
            keys = _key_columns(cur, table)
            join = " AND ".join(f"(to_jsonb(t) -> '{k}') = (c.row_key -> '{k}')" for k in keys)
            restricted = (
                "(SELECT jsonb_object_agg(e.key, e.value) FROM jsonb_each(to_jsonb(t)) e"
                " WHERE e.key = ANY (c.row_columns))"
            )
            found = cur.execute(
                f"SELECT c.seq, c.row_sha256, audit_row_sha256({restricted}), t IS NULL"  # noqa: S608
                f" FROM audit_chain c LEFT JOIN {table} t ON {join}"
                " WHERE c.table_name = %s ORDER BY c.seq",
                (table,),
            ).fetchall()
            for seq, recorded, now, gone in found:
                report.rows += 1
                if gone:
                    report.findings.append(f"{table}: the row of entry {seq} is missing")
                elif now != recorded:
                    report.findings.append(f"{table}: the row of entry {seq} was altered")
            loose = cur.execute(
                f"SELECT count(*) FROM {table} t WHERE NOT EXISTS"  # noqa: S608
                f" (SELECT 1 FROM audit_chain c WHERE c.table_name = %s AND {join})",
                (table,),
            ).fetchone()
            if loose is not None and int(loose[0]):
                report.findings.append(f"{table}: {loose[0]} rows were never chained")


def _checkpoints(
    db: ScopedDB,
    settings: Settings,
    by_seq: dict[int, str],
    report: AuditReport,
    signer: Signer | None,
    store: checkpoints.CheckpointStore | None,
) -> None:
    active = signer or signer_for(settings)
    exports = store or checkpoints.store_for(settings)
    for cp in checkpoints.recorded(db):
        report.checkpoints += 1
        label = f"checkpoint {cp.seq} ({cp.signed_at:%Y-%m-%d %H:%M})"
        if cp.key_id != active.key_id:
            report.findings.append(f"{label}: signed by {cp.key_id}, not {active.key_id}")
        elif not active.verify(canonical(cp.statement), unb64(cp.signature)):
            report.findings.append(f"{label}: the signature does not verify")
        if cp.statement.get("seq") != cp.seq or cp.statement.get("entry_sha256") != cp.entry_sha256:
            report.findings.append(f"{label}: the row says other than its signed statement")
        if by_seq.get(cp.seq) != cp.statement.get("entry_sha256"):
            report.findings.append(f"{label}: the chain no longer has the entry it signed")
        try:
            exported = json.loads(exports.get(cp.exported_to))
        except Exception as exc:  # gone, or unreadable
            report.findings.append(f"{label}: the export is unreadable ({type(exc).__name__})")
            continue
        if exported.get("statement") != cp.statement or exported.get("signature") != cp.signature:
            report.findings.append(f"{label}: the export says other than the database")


def verify(
    db: ScopedDB,
    settings: Settings,
    *,
    signer: Signer | None = None,
    store: checkpoints.CheckpointStore | None = None,
) -> AuditReport:
    report = AuditReport()
    by_seq = _chain(db, report)
    _rows(db, report)
    _checkpoints(db, settings, by_seq, report, signer, store)
    return report
