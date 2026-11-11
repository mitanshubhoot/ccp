"""LT6 (A-170): a method card on every verdict and suggestion, from what code recorded, and
a quality snapshot per AI workflow and release that withholds any false-PASS bound resting
on too few FAIL-labelled cases.

The defect: CCP published no quality figures, and nothing told a reader how a given
verdict was reached beyond the verdict itself.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from ulid import ULID

from ccp.config import Settings
from ccp.evals import publish
from ccp.evaluators import runner as evaluations
from ccp.evaluators.verdict import latest_per_subject, persist
from ccp.remediation import fixes, suggestions
from ccp.storage.scoped import ScopedDB
from tests.support.pipeline import prepared_tenants

NOW = datetime(2026, 9, 11, 12, tzinfo=UTC)


def _report(**targets: dict[str, Any]) -> dict[str, Any]:
    return {"golden_sha256": "abc123", "metrics": {"ai_tests": targets}}


def _stats(fail_labelled: int, false_rate: float, cases: int = 400) -> dict[str, Any]:
    return {
        "cases": cases,
        "runs_per_case": 5,
        "fail_labelled": fail_labelled,
        "false_pass": {"estimate": false_rate},
        "pass_k": {"estimate": 0.91},
    }


def test_a_bound_is_published_only_on_enough_fail_cases(tmp_path: Path) -> None:
    settings = Settings().model_copy(update={"ccp_eval_runs_dir": tmp_path / "runs"})
    (tmp_path / "runs").mkdir()
    (tmp_path / "runs" / "calibration-20260901T000000.json").write_text("{}", encoding="utf-8")
    report = _report(
        tier2_verdict=_stats(12, 0.0),
        tier3_verdict=_stats(320, 0.0),
        investigation=_stats(400, 0.0075),
    )
    snaps = {s.workflow: s for s in publish.snapshots(report, settings, NOW)}
    assert snaps["tier2_verdict"].false_pass_upper is None
    assert snaps["tier2_verdict"].withheld == "12 FAIL-labelled cases; at least 300 are needed"
    assert snaps["tier3_verdict"].false_pass_upper == pytest.approx(1 - 0.05 ** (1 / 320), abs=1e-4)
    assert snaps["tier3_verdict"].false_pass_upper < 0.01  # the Gate 3 bar
    assert (
        snaps["investigation"].false_passes == 3
        and snaps["investigation"].false_pass_upper > 0.0075
    )
    one = snaps["tier3_verdict"]
    assert one.quarter == "2026-Q3" and one.k == 5 and one.pass_k == 0.91
    assert one.dataset_sha256 == "abc123" and one.last_recertified == "2026-09-01"
    assert "tier2" in one.models and one.prompt_versions and one.method["min_fail_cases"] == 300
    path = tmp_path / "report.json"
    path.write_text(json.dumps(report), encoding="utf-8")
    written = publish.publish(path, settings, tmp_path / "published", NOW)
    assert {p.parent.name for p in written} == {"2026-Q3"} and len(written) == 3
    latest = publish.latest_published("tier3_verdict", tmp_path / "published")
    assert latest is not None and latest["false_pass_upper"] == one.false_pass_upper


@pytest.fixture(scope="module")
def world(base_settings: Settings) -> Iterator[tuple[Settings, ScopedDB]]:
    with prepared_tenants(base_settings, "ccp_cards", ("acme",)) as (settings, dbs):
        acme = dbs["acme"]
        evaluations.enqueue_all(acme, settings, now=NOW, tests=("HR-1",))
        evaluations.drain(acme, settings, now=NOW)
        yield settings, acme


@pytest.mark.db
def test_every_verdict_and_suggestion_has_a_method_card(
    world: tuple[Settings, ScopedDB], tmp_path: Path
) -> None:
    settings, acme = world
    rule = next(v for v in latest_per_subject(acme, "HR-1") if v.status.value == "fail")
    card = publish.verdict_card(acme, rule.verdict_id, tmp_path)
    assert card is not None and card["decided_by"] == "rule" and card["rule_version"]
    assert card["quality"] is None and card["bundle_id"]
    assert card["evidence"] and {e["provenance"] for e in card["evidence"]} <= {
        "system_observed",
        "customer_attested",
    }

    modelled = rule.model_copy(
        update={
            "verdict_id": str(ULID()),
            "tier": 2,
            "model": "stub:claude-haiku-5-5",
            "prompt_version": "tier2_v1",
            "evaluation_key": f"{rule.evaluation_key}:tier2",
        }
    )
    assert persist(acme, modelled)
    unpublished = publish.verdict_card(acme, modelled.verdict_id, tmp_path)
    assert unpublished is not None and unpublished["decided_by"] == "model"
    assert unpublished["quality"] == "no published figures for this workflow yet"
    folder = tmp_path / "2026-Q3"
    folder.mkdir()
    (folder / "tier2_verdict-b1.json").write_text(
        json.dumps(
            {
                "quarter": "2026-Q3",
                "bundle_id": "b1",
                "false_pass_upper": None,
                "withheld": "12 cases",
                "fail_labelled": 12,
                "pass_k": 0.9,
                "k": 5,
            }
        ),
        encoding="utf-8",
    )
    published = publish.verdict_card(acme, modelled.verdict_id, tmp_path)
    assert published is not None and published["quality"]["withheld"] == "12 cases"
    assert publish.verdict_card(acme, "nope", tmp_path) is None

    rendered = fixes.render(
        fixes.get("github_remove_collaborator"),
        {"login": "jsmith", "repo": "acme-example/platform"},
    )
    suggestion_id, _ = suggestions.suggest(
        acme,
        verdict_id=rule.verdict_id,
        test_id="HR-1",
        resource="jsmith",
        source="model",
        rendered=rendered,
    )
    fix = publish.suggestion_card(acme, suggestion_id)
    assert fix is not None and fix["chosen_by"] == "model, bounded to the catalogue"
    assert fix["rendered_by"].startswith("code") and fix["template_sha256"] == rendered.fix.sha256
