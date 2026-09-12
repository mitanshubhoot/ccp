"""Entity persistence: idempotent inserts, current views, RLS, and the entities topic.

Runs against its own scratch database so the counts are exactly the fixtures' and no
other module's queue records are in play.
"""

from __future__ import annotations

from collections.abc import Iterator

import psycopg
import pytest

from ccp.config import Settings
from ccp.ingest.landing import ingest_tenant
from ccp.normalize import repository
from ccp.normalize.consumer import normalize_all
from ccp.storage.db import app_pool, scratch_database
from ccp.storage.scoped import ScopedDB
from ccp.tenancy import TenantContext
from ccp.types import EmploymentStatus, EvidenceType, GrantState

pytestmark = pytest.mark.db


@pytest.fixture(scope="module")
def db_settings(base_settings: Settings) -> Iterator[Settings]:
    try:
        with scratch_database(base_settings, prefix="ccp_norm") as settings:
            yield settings
    except psycopg.OperationalError as exc:  # pragma: no cover
        pytest.skip(f"Postgres not reachable: {exc}")


@pytest.fixture(scope="module")
def normalized(db_settings: Settings) -> Iterator[dict[str, ScopedDB]]:
    pool = app_pool(db_settings, max_size=4)
    dbs = {t: ScopedDB(pool, TenantContext.for_id(db_settings, t)) for t in ("acme", "globex")}
    for scoped in dbs.values():
        ingest_tenant(db_settings, scoped, scoped.tenant)
        normalize_all(scoped, db_settings)
    try:
        yield dbs
    finally:
        pool.close()


def test_normalize_is_idempotent(normalized: dict[str, ScopedDB], db_settings: Settings) -> None:
    acme = normalized["acme"]
    before = acme.fetch_one("SELECT count(*) FROM persons")
    assert normalize_all(acme, db_settings) == 0  # cursor already at the end
    ingest_tenant(db_settings, acme, acme.tenant)  # everything dedups
    assert normalize_all(acme, db_settings) == 0
    assert acme.fetch_one("SELECT count(*) FROM persons") == before


def test_current_person_state_reflects_the_latest_observation(
    normalized: dict[str, ScopedDB],
) -> None:
    persons = {p.entity_id: p for p in repository.current_persons(normalized["acme"])}
    jane = persons["hris:person:E1042"]
    assert jane.employment_status is EmploymentStatus.terminated
    assert jane.emails == ("jsmith@acme.example",)
    assert persons["hris:account:ops-shared"].account_kind.value == "shared"
    assert persons["github:person:U_kgDOAcme1042"].handles == {"github": "jsmith"}


def test_current_grants_track_add_then_remove(normalized: dict[str, ScopedDB]) -> None:
    grants = {g.entity_id: g for g in repository.current_grants(normalized["acme"])}
    assert grants["github:grant:U_kgDOAcme1042:org:acme-example"].state is GrantState.active
    assert grants["github:grant:U_kgDOAcme1042:R_kgDOAcmeP001"].state is GrantState.active
    assert grants["github:grant:U_kgDOAcme1039:R_kgDOAcmeW002"].state is GrantState.revoked
    assert grants["github:grant:U_kgDOAcme1039:org:acme-example"].state is GrantState.revoked
    assert grants["hris:grant:okta-00u1042"].state is GrantState.revoked
    assert grants["hris:grant:okta-00u1044"].attributes["mfa_enabled"] is False


def test_asset_settings_merge_across_snapshots(normalized: dict[str, ScopedDB]) -> None:
    assets = {a.entity_id: a for a in repository.current_assets(normalized["acme"])}
    platform = assets["github:asset:R_kgDOAcmeP001"]
    assert platform.settings["default_branch"] == "main"
    assert platform.settings["branch_protection"]["required_approving_review_count"] == 2
    assert assets["hris:asset:acme-sys-payroll"].tags == ("critical",)


def test_evidence_by_type_and_page_texts(normalized: dict[str, ScopedDB]) -> None:
    globex = normalized["globex"]
    reviews = repository.current_evidence(globex, EvidenceType.access_review_report)
    assert [e.source_connector for e in reviews] == ["manual", "hris"]  # newest first
    assert reviews[0].page_texts is not None and len(reviews[0].page_texts) == 2
    training = repository.current_evidence(normalized["acme"], EvidenceType.training_record)
    assert {e.subject_entity_id for e in training} >= {"hris:person:E1058", "hris:person:E1050"}


def test_entities_topic_has_one_record_per_inserted_entity(normalized: dict[str, ScopedDB]) -> None:
    acme = normalized["acme"]
    total_rows = sum(
        acme.fetch_one(f"SELECT count(*) FROM {t}")[0]  # type: ignore[index]
        for t in ("persons", "access_grants", "assets", "evidence", "vendors")
    )
    assert acme.fetch_one("SELECT count(*) FROM event_log WHERE topic = 'entities'") == (
        total_rows,
    )


def test_entity_tables_and_views_are_tenant_scoped(
    normalized: dict[str, ScopedDB], db_settings: Settings
) -> None:
    acme, globex = normalized["acme"], normalized["globex"]
    assert all(p.tenant_id == "acme" for p in repository.current_persons(acme))
    assert all(p.tenant_id == "globex" for p in repository.current_persons(globex))
    assert {p.display_name for p in repository.current_persons(acme)} & {
        p.display_name for p in repository.current_persons(globex)
    } == {"Jane Smith"}
    with psycopg.connect(db_settings.database_url, autocommit=True) as conn:
        with pytest.raises(psycopg.errors.RaiseException):
            conn.execute("SELECT count(*) FROM current_persons").fetchone()
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        acme.execute("DELETE FROM evidence")  # scoped, so it is the grant that refuses


def test_two_tenants_same_entity_id_do_not_collide(normalized: dict[str, ScopedDB]) -> None:
    """Both tenants hold vendors and an identity-provider asset under identical entity ids."""
    for scoped in normalized.values():
        assert [v.entity_id for v in repository.current_vendors(scoped)] == [
            "github:vendor:github",
            "hris:vendor:hris",
        ]
        assert scoped.fetch_one("SELECT count(*) FROM current_vendors") == (2,)
