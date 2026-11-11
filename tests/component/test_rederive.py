"""MT8 (A-125): a tier-1 verdict re-derives exactly from what it read.

The defect: evaluators read the latest observations and verdicts cited entity ids, so a
verdict could not be re-derived from what it saw. A later observation of the same entity
changed what a re-run would read, and nothing recorded the tenant parameters, the clock or
the sync runs behind it.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import httpx
import pytest
from typer.testing import CliRunner

from ccp import cli
from ccp.config import Settings
from ccp.evaluators import rederive, rule_versions, runner
from ccp.evaluators.deterministic import gh2
from ccp.evaluators.verdict import Verdict, latest_per_subject
from ccp.observability import steps
from ccp.storage import sequences
from ccp.storage.scoped import ScopedDB
from ccp.types import VerdictStatus
from tests.component.test_github_recorded import ORG
from tests.component.test_required_sources import _gh2, _pull, _with_limit, own  # noqa: F401
from tests.support.pipeline import prepared_tenants

pytestmark = pytest.mark.db

ANA = {"login": "ana", "node_id": "U_ANA"}
BO = {"login": "bo", "node_id": "U_BO"}


@pytest.fixture(scope="module")
def world(base_settings: Settings) -> Iterator[tuple[Settings, dict[str, ScopedDB]]]:
    with prepared_tenants(base_settings, "ccp_rederive") as (settings, dbs):
        for db in dbs.values():
            runner.enqueue_all(db, settings, tiers=(1,))
            runner.drain(db, settings)
        yield settings, dbs


def test_every_tier1_verdict_of_a_full_run_rederives_exactly(
    world: tuple[Settings, dict[str, ScopedDB]],
) -> None:
    settings, dbs = world
    for db in dbs.values():
        results = rederive.sentinel(db, settings, sample=1000)
        assert len(results) >= 7  # every tier-1 subject of the fixture tenant
        assert [r for r in results if r.outcome != "exact"] == []
        recorded = db.fetch_all("SELECT DISTINCT outcome FROM replay_checks")
        assert [r[0] for r in recorded] == ["exact"]


def test_every_observation_is_stamped_below_the_watermark(
    world: tuple[Settings, dict[str, ScopedDB]],
) -> None:
    _, dbs = world
    acme = dbs["acme"]
    watermark = sequences.current_value(acme, sequences.OBSERVATIONS)
    for table in ("persons", "access_grants", "assets", "evidence", "person_links"):
        row = acme.fetch_one(
            f"SELECT count(*) FILTER (WHERE recorded_seq IS NULL), max(recorded_seq) FROM {table}"
        )
        assert row is not None and row[0] == 0, table  # nothing written since is unstamped
        assert row[1] is not None and row[1] <= watermark, table
    (verdict,) = latest_per_subject(acme, "GH-2")
    assert verdict.manifest is not None
    assert verdict.manifest["watermark"] <= watermark
    assert verdict.manifest["settings"]["org_admin_limit"] == 3
    assert set(verdict.manifest["sources"]) == {"github:members"}


def test_inputs_that_arrive_later_do_not_move_a_rederivation(
    own: tuple[Settings, ScopedDB],  # noqa: F811
) -> None:
    settings, acme = own
    strict = _with_limit(acme, 1)
    _pull(settings, strict)  # ana is the one admin
    first = _gh2(settings, strict)
    assert first.status is VerdictStatus.passed
    both = [httpx.Response(200, json=[ANA, BO]), httpx.Response(200, json=[ANA, BO])]
    _pull(settings, strict, overrides={f"/orgs/{ORG}/members": both})  # bo is an admin now
    assert _gh2(settings, strict).status is VerdictStatus.failed  # a fresh run sees bo
    again = rederive.rederive(strict, settings, first)
    assert (again.outcome, again.diff) == ("exact", ())  # it read only what it read


def test_a_changed_rule_is_named_and_a_drifting_rule_is_a_mismatch(
    world: tuple[Settings, dict[str, ScopedDB]], monkeypatch: pytest.MonkeyPatch
) -> None:
    settings, dbs = world
    (verdict,) = latest_per_subject(dbs["acme"], "GH-2")
    with monkeypatch.context() as m:
        m.setattr(rule_versions, "rule_version", lambda test_id: "a-later-rule")
        assert rederive.rederive(dbs["acme"], settings, verdict).outcome == "rule_changed"
    real = gh2.evaluate

    def drifting(ctx: Any, subject: str) -> Verdict:
        found = real(ctx, subject)
        return found.model_copy(update={"reasoning": found.reasoning + " (and the hour)"})

    monkeypatch.setattr(gh2, "evaluate", drifting)
    result = rederive.rederive(dbs["acme"], settings, verdict)
    assert result.outcome == "mismatch"
    assert result.diff and result.diff[0].startswith("reasoning:")


def test_ccp_replay_rederives_a_tier1_verdict(
    world: tuple[Settings, dict[str, ScopedDB]], monkeypatch: pytest.MonkeyPatch
) -> None:
    settings, dbs = world
    (verdict,) = latest_per_subject(dbs["acme"], "GH-2")

    def scratch() -> Settings:
        steps.configure(settings, sink=None)
        return settings

    monkeypatch.setattr(cli, "_settings", scratch)
    result = CliRunner().invoke(cli.app, ["replay", verdict.verdict_id, "--tenant", "acme"])
    assert result.exit_code == 0, result.output
    assert f"verdict {verdict.verdict_id} GH-2" in result.output and ": exact" in result.output
    sentinel = CliRunner().invoke(cli.app, ["evaluate", "sentinel", "--tenant", "acme"])
    assert sentinel.exit_code == 0, sentinel.output
    assert "mismatch" not in sentinel.output
