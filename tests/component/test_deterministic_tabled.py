"""Tier 1 tabled results on both tenants' fixtures, no network (spec.md Phase 7a)."""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from ccp.config import Settings
from ccp.evaluators import runner
from ccp.evaluators.verdict import Verdict, latest_per_subject
from ccp.storage.scoped import ScopedDB
from ccp.types import VerdictStatus
from tests.support.pipeline import prepared_tenants

pytestmark = pytest.mark.db

JANE = "person:hris:person:E1042"
YUKI = "person:hris:person:G2005"
ACME_ORG = "org:github:asset:org:acme-example"
GLOBEX_ORG = "org:github:asset:org:globex-example"
ACME_PLATFORM = "repo:github:asset:R_kgDOAcmeP001"
ACME_WEBSITE = "repo:github:asset:R_kgDOAcmeW002"
ACME_INFRA = "repo:github:asset:R_kgDOAcmeI003"
GLOBEX_PLATFORM = "repo:github:asset:R_kgDOGlobexP001"
GLOBEX_MOBILE = "repo:github:asset:R_kgDOGlobexM002"

TABLE: dict[tuple[str, str], dict[str, str]] = {
    ("acme", "HR-1"): {JANE: "fail"},
    ("acme", "GH-1"): {JANE: "fail"},
    ("acme", "GH-2"): {ACME_ORG: "pass"},
    ("acme", "GH-3"): {ACME_PLATFORM: "pass", ACME_WEBSITE: "partial", ACME_INFRA: "fail"},
    ("acme", "GH-5"): {ACME_PLATFORM: "fail", ACME_WEBSITE: "pass", ACME_INFRA: "needs_review"},
    ("acme", "HR-2"): {"tenant": "fail"},
    ("acme", "HR-5"): {"tenant": "fail"},
    ("globex", "HR-1"): {YUKI: "pass"},
    ("globex", "GH-1"): {YUKI: "pass"},
    ("globex", "GH-2"): {GLOBEX_ORG: "fail"},
    ("globex", "GH-3"): {GLOBEX_PLATFORM: "pass", GLOBEX_MOBILE: "fail"},
    ("globex", "GH-5"): {GLOBEX_PLATFORM: "pass", GLOBEX_MOBILE: "pass"},
    ("globex", "HR-2"): {"tenant": "fail"},
    ("globex", "HR-5"): {"tenant": "pass"},
}


@pytest.fixture(scope="module")
def world(base_settings: Settings) -> Iterator[tuple[Settings, dict[str, ScopedDB]]]:
    with prepared_tenants(base_settings, "ccp_tier1") as (settings, dbs):
        for db in dbs.values():
            runner.enqueue_all(db, settings, tiers=(1,))
            runner.drain(db, settings)
        yield settings, dbs


def _by_subject(db: ScopedDB, test_id: str) -> dict[str, Verdict]:
    return {v.subject: v for v in latest_per_subject(db, test_id)}


@pytest.mark.parametrize(("tenant", "test_id"), sorted(TABLE))
def test_tabled_result(
    world: tuple[Settings, dict[str, ScopedDB]], tenant: str, test_id: str
) -> None:
    got = _by_subject(world[1][tenant], test_id)
    assert {s: v.status.value for s, v in got.items()} == TABLE[(tenant, test_id)]
    for v in got.values():
        assert v.tier == 1 and v.model is None and v.confidence == 1.0
        assert v.trigger_reason == "full_run"


def test_jane_hr1_fail_cites_both_surviving_grants_and_the_sla_deadline(
    world: tuple[Settings, dict[str, ScopedDB]],
) -> None:
    v = _by_subject(world[1]["acme"], "HR-1")[JANE]
    assert v.status is VerdictStatus.failed
    assert v.evidence_refs == (
        "github:grant:U_kgDOAcme1042:R_kgDOAcmeP001",
        "github:grant:U_kgDOAcme1042:org:acme-example",
        "hris:person:E1042",
    )
    assert v.valid_from.isoformat() == "2026-08-21T17:00:00+00:00"  # termination + 24 h SLA
    assert "still active" in v.reasoning and "Jane Smith" in v.reasoning


def test_yuki_hr1_pass_saw_the_revocations_before_the_deadline(
    world: tuple[Settings, dict[str, ScopedDB]],
) -> None:
    v = _by_subject(world[1]["globex"], "HR-1")[YUKI]
    assert v.status is VerdictStatus.passed
    assert "hris:grant:okta-gx-2005" in v.evidence_refs
    assert v.valid_from.isoformat() == "2026-07-01T18:00:00+00:00"


def test_gh5_names_the_pattern_with_a_redacted_preview(
    world: tuple[Settings, dict[str, ScopedDB]],
) -> None:
    v = _by_subject(world[1]["acme"], "GH-5")[ACME_PLATFORM]
    assert "github_pat_classic" in v.reasoning and "ghp_A1..." in v.reasoning
    assert "ghp_A1b2C3d4" not in v.reasoning  # never the full token
    assert "secret_detected" in v.guardrail_events


def test_hr2_and_hr5_name_the_offenders(world: tuple[Settings, dict[str, ScopedDB]]) -> None:
    acme, globex = world[1]["acme"], world[1]["globex"]
    assert "lpark@acme.example" in _by_subject(acme, "HR-2")["tenant"].reasoning
    assert "kofi.mensah@globex.example" in _by_subject(globex, "HR-2")["tenant"].reasoning
    assert "ops-shared" in _by_subject(acme, "HR-5")["tenant"].reasoning
    assert "oncall-shared" not in _by_subject(acme, "HR-5")["tenant"].reasoning


def test_gh2_reasoning_counts_admins_against_the_tenant_limit(
    world: tuple[Settings, dict[str, ScopedDB]],
) -> None:
    acme = _by_subject(world[1]["acme"], "GH-2")[ACME_ORG]
    globex = _by_subject(world[1]["globex"], "GH-2")[GLOBEX_ORG]
    assert acme.reasoning.startswith("3 organization admins") and "limit of 3" in acme.reasoning
    assert globex.reasoning.startswith("3 organization admins") and "limit of 2" in globex.reasoning


def test_a_second_full_run_writes_nothing_new(
    world: tuple[Settings, dict[str, ScopedDB]],
) -> None:
    settings, dbs = world
    acme = dbs["acme"]
    before = acme.fetch_one("SELECT count(*) FROM verdicts")
    enqueued = runner.enqueue_all(acme, settings, tiers=(1,))
    summary = runner.drain(acme, settings)
    assert enqueued == summary.evaluated == summary.duplicates
    assert acme.fetch_one("SELECT count(*) FROM verdicts") == before
    assert acme.fetch_one("SELECT count(*) FROM event_log WHERE topic = 'verdicts'") == before


def test_verdicts_are_tenant_scoped(world: tuple[Settings, dict[str, ScopedDB]]) -> None:
    _, dbs = world
    assert all(v.tenant_id == "acme" for v in latest_per_subject(dbs["acme"]))
    assert not any(v.subject == YUKI for v in latest_per_subject(dbs["acme"]))
    assert not any(v.subject == JANE for v in latest_per_subject(dbs["globex"]))
