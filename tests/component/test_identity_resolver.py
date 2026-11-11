"""Resolver against a scratch database: decision log, links, review queue round-trip, isolation."""

from __future__ import annotations

from collections.abc import Iterator

import psycopg
import pytest

from ccp.approvals import actions as approval_actions
from ccp.approvals import store as approvals
from ccp.auth import members
from ccp.config import Settings
from ccp.identity import resolver
from ccp.ingest.landing import ingest_tenant
from ccp.normalize.consumer import normalize_all
from ccp.storage.db import app_pool, scratch_database
from ccp.storage.scoped import ScopedDB
from ccp.tenancy import TenantContext
from tests.support.members import member

pytestmark = pytest.mark.db


@pytest.fixture(scope="module")
def db_settings(base_settings: Settings) -> Iterator[Settings]:
    try:
        with scratch_database(base_settings, prefix="ccp_ident") as settings:
            yield settings
    except psycopg.OperationalError as exc:  # pragma: no cover
        pytest.skip(f"Postgres not reachable: {exc}")


@pytest.fixture(scope="module")
def dbs(db_settings: Settings) -> Iterator[dict[str, ScopedDB]]:
    pool = app_pool(db_settings, max_size=4)
    out = {t: ScopedDB(pool, TenantContext.for_id(db_settings, t)) for t in ("acme", "globex")}
    for scoped in out.values():
        ingest_tenant(db_settings, scoped, scoped.tenant)
        normalize_all(scoped, db_settings)
    try:
        yield out
    finally:
        pool.close()


def test_resolve_logs_every_comparison_and_is_idempotent(dbs: dict[str, ScopedDB]) -> None:
    acme = dbs["acme"]
    first = resolver.resolve_tenant(acme)
    assert first.compared >= 8
    assert first.routes.get("blocked", 0) == 3  # ci-runner, ops-shared, oncall-shared
    assert first.routes.get("review", 0) >= 1
    assert first.routes.get("auto_merge", 0) >= 6
    second = resolver.resolve_tenant(acme)
    assert second.compared == 0 and second.skipped_unchanged == first.compared
    rows = acme.fetch_all("SELECT gate_result, route, score FROM resolution_decisions")
    assert len(rows) == first.compared
    assert all(r[2] is None for r in rows if r[0] != "passed")


def test_links_point_both_sides_at_the_hris_person(dbs: dict[str, ScopedDB]) -> None:
    acme = dbs["acme"]
    links = resolver.canonical_map(acme)
    assert links["github:person:U_kgDOAcme1042"] == "hris:person:E1042"
    assert links["hris:person:E1042"] == "hris:person:E1042"
    assert resolver.canonical_id(acme, "github:person:U_kgDOAcme1042") == "hris:person:E1042"
    assert (
        resolver.canonical_id(acme, "github:person:U_kgDOAcmeBot01")
        == "github:person:U_kgDOAcmeBot01"
    )


def test_review_queue_round_trip(db_settings: Settings, dbs: dict[str, ScopedDB]) -> None:
    acme = dbs["acme"]
    items = resolver.open_reviews(acme)
    dana = next(i for i in items if i.right_entity_id == "hris:person:E1017")
    assert dana.left_entity_id == "github:person:U_kgDOAcme1017"
    assert "hris:person:E1017" in resolver.pending_review_entities(acme)
    item = approvals.for_ref(acme, "identity_merge", dana.decision_id)  # opened with it (A-132)
    assert item is not None and item.is_open and item.requested_by == resolver.IDENTITY_RESOLVER
    assert item.payload["right"]["entity_id"] == "hris:person:E1017"
    with pytest.raises(members.NotPermitted):  # a typed name decides nothing
        approvals.record_event(acme, item.approval_id, "approved", "dana.reviewer")
    with pytest.raises(members.NotPermitted):  # a reviewer may not link two people
        approval_actions.approve(
            acme, db_settings, item.approval_id, member(acme, "rui", "reviewer")
        )
    approval_actions.approve(
        acme,
        db_settings,
        item.approval_id,
        member(acme, "dana.reviewer"),
        "same person, confirmed",
    )
    assert dana.decision_id not in {i.decision_id for i in resolver.open_reviews(acme)}
    assert resolver.canonical_id(acme, "github:person:U_kgDOAcme1017") == "hris:person:E1017"
    assert "hris:person:E1017" not in resolver.pending_review_entities(acme)
    with pytest.raises(ValueError):  # decided once
        approval_actions.reject(
            acme, db_settings, item.approval_id, member(acme, "dana.reviewer"), "again"
        )
    row = acme.fetch_one(
        "SELECT decided_by FROM current_person_links WHERE person_entity_id = %s",
        ("github:person:U_kgDOAcme1017",),
    )
    assert row == ("human",)
    resolution = acme.fetch_one(
        "SELECT outcome, reviewer, approval_id FROM review_resolutions WHERE decision_id = %s",
        (dana.decision_id,),
    )
    assert resolution == ("merge", "dana.reviewer", item.approval_id)


def test_blocked_pairs_are_logged_with_no_score(dbs: dict[str, ScopedDB]) -> None:
    acme = dbs["acme"]
    resolver.resolve_tenant(acme)  # idempotent; makes this test order-independent
    rows = acme.fetch_all(
        "SELECT left_entity_id, right_entity_id, gate_result FROM resolution_decisions"
        " WHERE route = 'blocked' ORDER BY 1, 2"
    )
    assert rows == [
        ("github:person:U_kgDOAcmeBot01", "hris:person:E1023", "blocked_kind_mismatch"),
        ("github:person:U_kgDOAcmeShar1", "hris:account:oncall-shared", "blocked_shared_account"),
        ("github:person:U_kgDOAcmeShar1", "hris:account:ops-shared", "blocked_shared_account"),
    ]


def test_globex_jane_never_links_to_acme_jane(dbs: dict[str, ScopedDB]) -> None:
    globex = dbs["globex"]
    resolver.resolve_tenant(globex)
    links = resolver.canonical_map(globex)
    assert links["github:person:U_kgDOGlobex2001"] == "hris:person:G2001"
    assert not any("Acme" in v or "E1042" in v for v in links.values())
    assert globex.fetch_one(
        "SELECT count(*) FROM resolution_decisions WHERE left_entity_id LIKE '%%Acme%%'"
    ) == (0,)
    assert dbs["acme"].fetch_one(
        "SELECT count(*) FROM resolution_decisions WHERE left_entity_id LIKE '%%Globex%%'"
    ) == (0,)


def test_decision_tables_are_append_only(dbs: dict[str, ScopedDB]) -> None:
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        dbs["acme"].execute("UPDATE person_links SET canonical_person_id = 'x'")
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        dbs["acme"].execute("DELETE FROM resolution_decisions")
