"""MT8 (A-124): the full run records each test's expected subjects, and the rollup reads them
with the parked evaluations, through the application role and the agent role alike."""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from ccp.compliance_state import reader, writer
from ccp.config import Settings
from ccp.evaluators import inventory, runner
from ccp.knowledge import graph as knowledge
from ccp.knowledge import rollup as knowledge_rollup
from ccp.storage.db import agent_pool
from ccp.storage.scoped import ScopedDB
from tests.support.pipeline import prepared_tenants

pytestmark = pytest.mark.db

MOBILE = "GH-5|repo:github:asset:R_kgDOGlobexM002"
PLATFORM = "GH-5|repo:github:asset:R_kgDOGlobexP001"


@pytest.fixture(scope="module")
def world(base_settings: Settings) -> Iterator[tuple[Settings, ScopedDB]]:
    with prepared_tenants(base_settings, "ccp_inventory", ("globex",)) as (settings, dbs):
        yield settings, dbs["globex"]


def test_the_full_run_records_each_tests_population(world: tuple[Settings, ScopedDB]) -> None:
    settings, globex = world
    runner.enqueue_all(globex, settings, tiers=(1,))
    covered = inventory.coverage(globex)
    assert covered.inventories["GH-5"].subjects == {MOBILE, PLATFORM}
    assert covered.inventories["GH-5"].complete  # the fixture pull listed the repos whole
    assert covered.inventories["HR-2"].subjects == {"HR-2|tenant"}
    assert "GH-4" not in covered.inventories  # tier 2 was not part of this run


def test_a_parked_evaluation_keeps_its_requirement_from_passing(
    world: tuple[Settings, ScopedDB],
) -> None:
    settings, globex = world
    runner.enqueue_all(globex, settings, tiers=(1,))
    globex.execute(
        "UPDATE event_log SET status = 'parked' WHERE topic = 'evaluations'"
        " AND status = 'pending' AND partition_key = %s",
        (f"globex:{MOBILE}",),
    )
    runner.drain(globex, settings)
    writer.drain(globex)
    graph = knowledge.load()
    heads = reader.heads(globex)
    plain = {
        r.requirement_id: r for r in knowledge_rollup.rollup(graph, heads, "iso27001").requirements
    }
    covered = {
        r.requirement_id: r for r in reader.framework_rollup(globex, "iso27001").requirements
    }
    secrets = [rid for rid, r in plain.items() if r.tests == ("GH-5",)]
    assert secrets, "a requirement evidenced by GH-5 alone"
    for rid in secrets:
        assert plain[rid].state == "pass"  # the heads alone: the parked repository is invisible
        assert covered[rid].state == "pending"
        assert covered[rid].pending_reasons == {"parked": 1}
        assert covered[rid].blocking_subject_key == MOBILE


def test_the_agent_role_reads_the_same_coverage(world: tuple[Settings, ScopedDB]) -> None:
    settings, globex = world
    runner.enqueue_all(globex, settings, tiers=(1,))
    pool = agent_pool(settings)
    try:
        as_agent = inventory.coverage(ScopedDB(pool, globex.tenant))
    finally:
        pool.close()
    assert as_agent == inventory.coverage(globex, as_agent.now)
    assert as_agent.inventories  # it could read them
