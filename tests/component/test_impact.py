"""The impact step: exactly the tabled (test, subject) pairs, and ten changes coalesce to one."""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest

from ccp.config import Settings
from ccp.evaluators import impact
from ccp.evaluators.impact import EvaluationRequest, ImpactConsumer, requests_for_entity
from ccp.ingest import event_log
from ccp.storage.scoped import ScopedDB
from tests.support.pipeline import prepared_tenants

pytestmark = pytest.mark.db

TS = datetime(2026, 9, 1, tzinfo=UTC)
JANE = "person:hris:person:E1042"


@pytest.fixture(scope="module")
def world(base_settings: Settings) -> Iterator[tuple[Settings, dict[str, ScopedDB]]]:
    with prepared_tenants(base_settings, "ccp_impact", ("acme",)) as (settings, dbs):
        yield settings, dbs


def _pairs(requests: list[EvaluationRequest]) -> set[tuple[str, str]]:
    return {(r.test_id, r.subject) for r in requests}


@pytest.mark.parametrize(
    ("table", "entity_id", "expected"),
    [
        ("persons", "hris:person:E1042", {("HR-1", JANE), ("GH-1", JANE)}),
        ("persons", "hris:person:E1001", set()),  # active employee: nothing to check
        ("persons", "github:person:U_kgDOAcme1042", set()),  # the grant, not the account, triggers
        ("person_links", "github:person:U_kgDOAcme1042", {("HR-1", JANE), ("GH-1", JANE)}),
        (
            "access_grants",
            "github:grant:U_kgDOAcme1042:org:acme-example",
            {("HR-1", JANE), ("GH-1", JANE), ("GH-2", "org:github:asset:org:acme-example")},
        ),
        (
            "access_grants",
            "github:grant:U_kgDOAcme1042:R_kgDOAcmeP001",
            {("HR-1", JANE), ("GH-1", JANE)},
        ),
        (
            "access_grants",
            "github:grant:U_kgDOAcme1001:org:acme-example",
            {("GH-2", "org:github:asset:org:acme-example")},
        ),
        ("access_grants", "hris:grant:okta-00u1044", set()),
        ("assets", "github:asset:R_kgDOAcmeP001", {("GH-3", "repo:github:asset:R_kgDOAcmeP001")}),
        (
            "assets",
            "github:asset:org:acme-example",
            {("GH-2", "org:github:asset:org:acme-example")},
        ),
        ("assets", "hris:asset:acme-sys-payroll", {("HR-4", "tenant")}),
        (
            "evidence",
            "github:evidence:R_kgDOAcmeP001:security",
            {
                ("GH-4", "repo:github:asset:R_kgDOAcmeP001"),
                ("GH-5", "repo:github:asset:R_kgDOAcmeP001"),
            },
        ),
        (
            "evidence",
            "github:evidence:R_kgDOAcmeP001:files",
            {("GH-5", "repo:github:asset:R_kgDOAcmeP001")},
        ),
        (
            "evidence",
            "github:evidence:R_kgDOAcmeI003:protection",
            {("GH-5", "repo:github:asset:R_kgDOAcmeI003")},
        ),
        ("evidence", "hris:evidence:okta-00u1044:mfa", {("HR-2", "tenant")}),
        ("evidence", "hris:evidence:doc:training-E1050.md", {("HR-3", "person:hris:person:E1050")}),
        ("evidence", "hris:evidence:doc:access-review-2026-q2.md", {("HR-4", "tenant")}),
        ("evidence", "hris:evidence:doc:critical-systems-list.md", {("HR-4", "tenant")}),
        ("evidence", "hris:evidence:acme-acct-0007:registry", {("HR-5", "tenant")}),
        ("evidence", "hris:evidence:E1042:roster", set()),  # the person row carries this change
        ("evidence", "github:evidence:U_kgDOAcme1042:membership", set()),
    ],
)
def test_impact_table_row(
    world: tuple[Settings, dict[str, ScopedDB]],
    table: str,
    entity_id: str,
    expected: set[tuple[str, str]],
) -> None:
    acme = world[1]["acme"]
    assert _pairs(requests_for_entity(acme, table, entity_id, TS)) == expected


ACME_PAIRS = {
    ("HR-1", JANE),
    ("GH-1", JANE),
    ("GH-2", "org:github:asset:org:acme-example"),
    ("GH-3", "repo:github:asset:R_kgDOAcmeP001"),
    ("GH-3", "repo:github:asset:R_kgDOAcmeW002"),
    ("GH-3", "repo:github:asset:R_kgDOAcmeI003"),
    ("GH-5", "repo:github:asset:R_kgDOAcmeP001"),
    ("GH-5", "repo:github:asset:R_kgDOAcmeW002"),
    ("GH-5", "repo:github:asset:R_kgDOAcmeI003"),
    ("GH-4", "repo:github:asset:R_kgDOAcmeP001"),
    ("GH-4", "tenant"),
    ("HR-2", "tenant"),
    ("HR-3", "person:hris:person:E1001"),
    ("HR-3", "person:hris:person:E1017"),
    ("HR-3", "person:hris:person:E1023"),
    ("HR-3", "person:hris:person:E1031"),
    ("HR-3", "person:hris:person:E1044"),
    ("HR-3", "person:hris:person:E1050"),
    ("HR-3", "person:hris:person:E1058"),
    ("HR-4", "tenant"),
    ("HR-5", "tenant"),
}


def test_draining_the_entities_topic_enqueues_exactly_the_tabled_pairs(
    world: tuple[Settings, dict[str, ScopedDB]],
) -> None:
    acme = world[1]["acme"]
    consumer = ImpactConsumer(acme)
    while consumer.run_once():
        pass
    rows = acme.fetch_all("SELECT partition_key, ref FROM event_log WHERE topic = 'evaluations'")
    pairs = {(json.loads(r[1])["test_id"], json.loads(r[1])["subject"]) for r in rows}
    assert pairs == ACME_PAIRS
    assert len(rows) == len(ACME_PAIRS)  # every entity row inside the window coalesced
    assert consumer.coalesced > 0 and consumer.enqueued == len(ACME_PAIRS)
    # the coalesced record carries the latest trigger seen for its subject
    latest = json.loads(
        acme.fetch_one(
            "SELECT ref FROM event_log WHERE topic = 'evaluations' AND partition_key = %s",
            ("acme:HR-2|tenant",),
        )[0]  # type: ignore[index]
    )
    assert latest["trigger_observed_at"] == "2026-08-25T08:00:00+00:00"


def test_ten_changes_to_one_subject_enqueue_one_evaluation(
    world: tuple[Settings, dict[str, ScopedDB]],
) -> None:
    acme = world[1]["acme"]
    subject = "person:hris:person:E9999"
    before = acme.fetch_one(
        "SELECT count(*) FROM event_log WHERE topic = 'evaluations' AND partition_key = %s",
        (f"acme:HR-1|{subject}",),
    )
    with acme.transaction() as cur:
        appended = [
            impact.enqueue(
                cur, "acme", EvaluationRequest("HR-1", subject, TS + timedelta(minutes=i))
            )
            for i in range(10)
        ]
    assert appended == [True] + [False] * 9
    rows = acme.fetch_all(
        "SELECT ref FROM event_log WHERE topic = 'evaluations' AND partition_key = %s",
        (f"acme:HR-1|{subject}",),
    )
    assert len(rows) == (before[0] if before else 0) + 1
    assert EvaluationRequest.from_ref(rows[-1][0]).trigger_observed_at == TS + timedelta(minutes=9)


def test_coalescing_is_per_tenant_and_per_subject(
    world: tuple[Settings, dict[str, ScopedDB]],
) -> None:
    acme = world[1]["acme"]
    with acme.transaction() as cur:
        first = impact.enqueue(cur, "acme", EvaluationRequest("GH-2", "org:x", TS))
        other_test = impact.enqueue(cur, "acme", EvaluationRequest("GH-3", "org:x", TS))
        other_subject = impact.enqueue(cur, "acme", EvaluationRequest("GH-2", "org:y", TS))
        same = impact.enqueue(cur, "acme", EvaluationRequest("GH-2", "org:x", TS))
    assert (first, other_test, other_subject, same) == (True, True, True, False)


def test_window_zero_disables_coalescing(world: tuple[Settings, dict[str, ScopedDB]]) -> None:
    acme = world[1]["acme"]
    with acme.transaction() as cur:
        results = [
            impact.enqueue(cur, "acme", EvaluationRequest("GH-2", "org:z", TS), coalesce_seconds=0)
            for _ in range(3)
        ]
    assert results == [True, True, True]


def test_entity_ref_parsing_survives_colons_in_ids(
    world: tuple[Settings, dict[str, ScopedDB]],
) -> None:
    acme = world[1]["acme"]
    with acme.transaction() as cur:
        offset = event_log.append(
            cur,
            tenant_id="acme",
            topic="entities",
            partition_key="acme:hris:evidence:doc:x.md",
            source_ts=TS,
            ref="evidence:hris:evidence:doc:x.md:training_record:x.md:abc123",
        )
        record = event_log.get(cur, offset)
        assert record is not None
    assert impact.parse_entity_ref(record) == ("evidence", "hris:evidence:doc:x.md")
