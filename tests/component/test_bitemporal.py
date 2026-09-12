"""The bitemporal worked example (spec.md Layer 5), the SLA-derived row, append-only proofs,
and the two-writer serialization test."""

from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path

import psycopg
import pytest
from psycopg_pool import ConnectionPool

from ccp.config import Settings
from ccp.storage import bitemporal
from ccp.storage.scoped import ScopedDB
from ccp.tenancy import TenantContext
from ccp.types import VerdictStatus

pytestmark = pytest.mark.db

MAR = {d: datetime(2026, 3, d, 12, 0, tzinfo=UTC) for d in range(1, 15)}


def stub_verdict(db: ScopedDB, verdict_id: str, test_id: str, subject_key: str, status: str) -> str:
    """A minimal verdict row so the state table's foreign key is satisfied."""
    db.execute(
        "INSERT INTO verdicts (verdict_id, evaluation_key, tenant_id, test_id, subject_key, status,"
        " reasoning, confidence, confidence_cap, tier, trigger_observed_at, valid_from)"
        " VALUES (%s, %s, %s, %s, %s, %s, %s, 1.0, 1.0, 1, now(), now()) ON CONFLICT DO NOTHING",
        (
            verdict_id,
            f"key:{verdict_id}",
            db.tenant.tenant_id,
            test_id,
            subject_key,
            status,
            "stub",
        ),
    )
    return verdict_id


@pytest.fixture
def acme(db_settings: Settings, pool: ConnectionPool) -> ScopedDB:
    return ScopedDB(pool, TenantContext.for_id(db_settings, "acme"))


def test_worked_example_five_queries(acme: ScopedDB) -> None:
    """spec.md Layer 5: pass Mar 1, late fail from Mar 4 recorded Mar 10, correction to
    fail-since-Mar-1 recorded Mar 12. Recorded times are simulated by direct inserts
    through the same SQL the writer uses, because the example fixes transaction time."""
    subject = "GH-3|repo:acme-example/platform"
    v1 = stub_verdict(acme, "v-ex-1", "GH-3", subject, "pass")
    v2 = stub_verdict(acme, "v-ex-2", "GH-3", subject, "fail")
    v3 = stub_verdict(acme, "v-ex-3", "GH-3", subject, "fail")
    with acme.transaction() as cur:
        for row_id, status, vid, valid_from, recorded, supersedes, reason, seq in (
            ("r1", "pass", v1, MAR[1], MAR[1], None, "evaluation", 1),
            ("r2", "fail", v2, MAR[4], MAR[10], "r1", "late_event", 2),
            ("r3", "fail", v3, MAR[1], MAR[12], "r2", "correction", 3),
        ):
            cur.execute(
                "INSERT INTO compliance_state (row_id, tenant_id, subject_key, test_id,"
                " control_ids, status, verdict_id, valid_from, valid_to, recorded_seq,"
                " recorded_at, supersedes_row_id, reason)"
                " VALUES (%s, 'acme', %s, 'GH-3', '[\"CTL-CHANGE-REVIEW\"]',"
                " %s, %s, %s, NULL, %s, %s, %s, %s)",
                (
                    row_id,
                    subject,
                    status,
                    vid,
                    valid_from,
                    1000 + seq,
                    recorded,
                    supersedes,
                    reason,
                ),
            )

    def status_at(valid_at: datetime, known_at: datetime) -> tuple[str, str] | None:
        row = bitemporal.state_as_of(acme, subject, valid_at, known_at)
        return (row.status.value, row.row_id) if row else None

    assert status_at(MAR[5], MAR[5]) == ("pass", "r1")  # believed_state(Mar 5)
    assert status_at(MAR[5], MAR[14]) == ("fail", "r3")  # current_state(valid_at=Mar 5)
    assert status_at(MAR[11], MAR[11]) == ("fail", "r2")  # believed_state(Mar 11)
    assert status_at(MAR[2], MAR[11]) == ("pass", "r1")  # r2 only claims validity from Mar 4
    assert status_at(MAR[2], MAR[13]) == ("fail", "r3")  # r3 rewrote history back to Mar 1
    assert bitemporal.believed_state(acme, subject, MAR[5]) is not None
    assert [r.row_id for r in bitemporal.chain(acme, subject)] == ["r1", "r2", "r3"]
    assert [h.row_id for h in bitemporal.heads(acme) if h.subject_key == subject] == ["r3"]


def test_writer_supersedes_the_head_and_stamps_a_monotonic_sequence(acme: ScopedDB) -> None:
    subject = "GH-2|org:acme-example"
    stub_verdict(acme, "v-w-1", "GH-2", subject, "pass")
    stub_verdict(acme, "v-w-2", "GH-2", subject, "fail")
    first = bitemporal.record(
        acme,
        subject_key=subject,
        test_id="GH-2",
        control_ids=("CTL-PRIV-LIMIT",),
        status="pass",
        verdict_id="v-w-1",
        valid_from=MAR[1],
    )
    second = bitemporal.record(
        acme,
        subject_key=subject,
        test_id="GH-2",
        control_ids=("CTL-PRIV-LIMIT",),
        status="fail",
        verdict_id="v-w-2",
        valid_from=MAR[3],
        reason="late_event",
    )
    assert second.supersedes_row_id == first.row_id
    assert second.recorded_seq > first.recorded_seq
    assert bitemporal.current_state(acme, subject, MAR[2]) is not None
    assert bitemporal.current_state(acme, subject, MAR[2]).status is VerdictStatus.passed  # type: ignore[union-attr]
    assert bitemporal.current_state(acme, subject, MAR[4]).status is VerdictStatus.failed  # type: ignore[union-attr]


def test_sla_derived_valid_from_for_hr1(acme: ScopedDB) -> None:
    """HR-1's violation is true from termination plus the SLA, not from any observation."""
    termination = datetime(2026, 8, 20, 17, 0, tzinfo=UTC)
    sla_hours = acme.tenant.settings.termination_revocation_sla_hours
    valid_from = termination + timedelta(hours=sla_hours)
    subject = "HR-1|person:hris:person:E1042"
    stub_verdict(acme, "v-hr1", "HR-1", subject, "fail")
    bitemporal.record(
        acme,
        subject_key=subject,
        test_id="HR-1",
        control_ids=("CTL-ACCESS-REVOKE",),
        status="fail",
        verdict_id="v-hr1",
        valid_from=valid_from,
    )
    assert bitemporal.current_state(acme, subject, termination + timedelta(hours=23)) is None
    row = bitemporal.current_state(acme, subject, termination + timedelta(hours=25))
    assert row is not None and row.status is VerdictStatus.failed
    assert row.valid_from == datetime(2026, 8, 21, 17, 0, tzinfo=UTC)


def test_state_table_is_append_only_at_the_database(acme: ScopedDB) -> None:
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        acme.execute("UPDATE compliance_state SET status = 'pass'")
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        acme.execute("DELETE FROM compliance_state")
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        acme.execute("UPDATE verdicts SET status = 'pass'")


def test_module_never_updates_or_deletes_state_rows() -> None:
    """The only write verb against compliance_state or verdicts in the writer is INSERT.
    (The per-tenant counter upserts tenant_sequences, which is an operational counter.)"""
    source = Path(bitemporal.__file__).read_text(encoding="utf-8")
    assert not re.search(r"\b(UPDATE|DELETE)\s+(FROM\s+)?(compliance_state|verdicts)\b", source)
    assert not re.search(r"\bDELETE\b", source)
    inserts = re.findall(r"INSERT INTO (\w+)", source)
    assert set(inserts) == {"tenant_sequences", "compliance_state"}


def test_a_row_can_be_superseded_only_once(acme: ScopedDB) -> None:
    subject = "GH-5|repo:acme-example/website"
    stub_verdict(acme, "v-once", "GH-5", subject, "pass")
    head = bitemporal.record(
        acme,
        subject_key=subject,
        test_id="GH-5",
        control_ids=("CTL-SECRETS",),
        status="pass",
        verdict_id="v-once",
        valid_from=MAR[1],
    )
    with pytest.raises(psycopg.errors.UniqueViolation):
        acme.execute(
            "INSERT INTO compliance_state (row_id, tenant_id, subject_key, test_id, control_ids,"
            " status, verdict_id, valid_from, recorded_seq, recorded_at, supersedes_row_id, reason)"
            " VALUES ('dup-a', 'acme', %s, 'GH-5', '[]', 'fail', 'v-once', now(), 9001, now(), %s,"
            " 'correction'), ('dup-b', 'acme', %s, 'GH-5', '[]', 'fail', 'v-once', now(), 9002,"
            " now(), %s, 'correction')",
            (subject, head.row_id, subject, head.row_id),
        )


def test_two_concurrent_writers_produce_one_chain(db_settings: Settings) -> None:
    subject = "HR-2|tenant"
    writers = 2
    per_writer = 8
    pools = [
        ConnectionPool(db_settings.database_url, min_size=1, max_size=2, open=True)
        for _ in range(writers)
    ]
    try:
        dbs = [ScopedDB(p, TenantContext.for_id(db_settings, "acme")) for p in pools]
        stub_verdict(dbs[0], "v-cc", "HR-2", subject, "pass")

        def work(db: ScopedDB) -> list[int]:
            seqs = []
            for i in range(per_writer):
                row = bitemporal.record(
                    db,
                    subject_key=subject,
                    test_id="HR-2",
                    control_ids=("CTL-MFA",),
                    status="pass" if i % 2 else "fail",
                    verdict_id="v-cc",
                    valid_from=MAR[1] + timedelta(minutes=i),
                )
                seqs.append(row.recorded_seq)
            return seqs

        with ThreadPoolExecutor(max_workers=writers) as ex:
            results = list(ex.map(work, dbs))
        chain = bitemporal.chain(dbs[0], subject)
        assert len(chain) == writers * per_writer
        seqs = [r.recorded_seq for r in chain]
        assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs)
        assert chain[0].supersedes_row_id is None
        assert [r.supersedes_row_id for r in chain[1:]] == [r.row_id for r in chain[:-1]]
        assert len([h for h in bitemporal.heads(dbs[0]) if h.subject_key == subject]) == 1
        all_seqs = sorted(s for r in results for s in r)
        assert all_seqs == seqs
    finally:
        for p in pools:
            p.close()
