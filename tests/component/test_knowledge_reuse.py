"""One verdict row backs both frameworks (spec.md Layer 6 reuse), and the loader is idempotent."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from psycopg_pool import ConnectionPool

from ccp.config import Settings
from ccp.knowledge import graph as kg
from ccp.knowledge.rollup import rollup
from ccp.storage import bitemporal
from ccp.storage.scoped import ScopedDB
from ccp.tenancy import TenantContext

pytestmark = pytest.mark.db


def test_loader_upserts_reference_tables(db_settings: Settings, pool: ConnectionPool) -> None:
    db = ScopedDB(pool, TenantContext.for_id(db_settings, "acme"))
    graph = kg.load()
    first = kg.load_into_db(db, graph)
    second = kg.load_into_db(db, graph)
    assert first == second
    assert db.fetch_one("SELECT count(*) FROM tests") == (14,)  # AWS-1 to AWS-3 (A-140)
    assert db.fetch_one("SELECT count(*) FROM requirements") == (18,)
    assert db.fetch_one("SELECT count(*) FROM requirement_controls") == (23,)
    assert db.fetch_one("SELECT count(*) FROM control_tests") == (14,)
    assert db.fetch_one("SELECT tier FROM tests WHERE test_id = 'HR-4'") == (3,)


def test_one_verdict_backs_two_frameworks(db_settings: Settings, pool: ConnectionPool) -> None:
    db = ScopedDB(pool, TenantContext.for_id(db_settings, "acme"))
    graph = kg.load()
    subject = "HR-1|person:hris:person:E1042"
    db.execute(
        "INSERT INTO verdicts (verdict_id, evaluation_key, tenant_id, test_id, subject_key, status,"
        " reasoning, confidence, confidence_cap, tier, trigger_observed_at, valid_from)"
        " VALUES ('v-reuse', 'k-reuse', 'acme', 'HR-1', %s, 'fail', 'stub', 1, 1, 1, now(), now())"
        " ON CONFLICT DO NOTHING",
        (subject,),
    )
    bitemporal.record(
        db,
        subject_key=subject,
        test_id="HR-1",
        control_ids=tuple(c.control_id for c in graph.controls_for_test("HR-1")),
        status="fail",
        verdict_id="v-reuse",
        valid_from=datetime(2026, 8, 21, 17, tzinfo=UTC),
    )
    heads = [h for h in bitemporal.heads(db) if h.verdict_id == "v-reuse"]
    assert len(heads) == 1 and heads[0].control_ids == ("CTL-ACCESS-REVOKE",)
    soc2 = {r.requirement_id: r for r in rollup(graph, heads, "soc2").requirements}
    iso = {r.requirement_id: r for r in rollup(graph, heads, "iso27001").requirements}
    assert soc2["CC6.2"].verdict_ids == ("v-reuse",) and soc2["CC6.3"].verdict_ids == ("v-reuse",)
    assert iso["A.5.18"].verdict_ids == ("v-reuse",)
    assert soc2["CC6.2"].state == iso["A.5.18"].state == "fail"
    assert soc2["CC6.2"].blocking_test_id == iso["A.5.18"].blocking_test_id == "HR-1"
