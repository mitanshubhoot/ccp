from __future__ import annotations

import json
from pathlib import Path

import pytest

from ccp.config import Settings
from ccp.evals import runner
from ccp.evals.schema import GoldenCase


def test_golden_files_load_with_unique_ids(base_settings: Settings) -> None:
    cases, hashes = runner.load_cases(base_settings.ccp_golden_dir)
    assert len(cases) >= 10
    assert set(hashes) == {"component.yaml", "workflow.yaml"}
    assert {c.level for c in cases} >= {"component", "workflow"}


def test_selection_filters(base_settings: Settings) -> None:
    cases, _ = runner.load_cases(base_settings.ccp_golden_dir)
    assert all(c.level == "workflow" for c in runner.select(cases, level="workflow"))
    assert runner.select(cases, case_id="cmp-connector-hris-acme-counts")[0].tenant == "acme"
    assert all("github" in c.tags for c in runner.select(cases, tag="github"))


def test_unknown_target_is_a_failed_case_not_a_crash(base_settings: Settings) -> None:
    case = GoldenCase(
        id="x", level="component", difficulty="easy", source="manual", target="nope", expected={}
    )
    result = runner.run_case(runner.EvalContext(settings=base_settings), case)
    assert not result.passed and result.error is not None and "unknown target" in result.error


def test_component_cases_pass_without_a_database_and_write_a_report(
    base_settings: Settings, tmp_path: Path
) -> None:
    cases, hashes = runner.load_cases(base_settings.ccp_golden_dir)
    chosen = runner.select(cases, level="component")
    report = runner.run(base_settings, chosen, hashes)
    failed = [(r.id, r.mismatches, r.error) for r in report.cases if not r.passed]
    assert failed == []
    path = runner.write_report(report, tmp_path)
    latest = json.loads((tmp_path / "latest.json").read_text(encoding="utf-8"))
    assert latest["run_id"] == report.run_id and path.exists()
    assert latest["summary"]["component"]["total"] == len(chosen)
    assert "unit" in runner.format_table(report)


@pytest.mark.db
def test_workflow_cases_pass_against_a_scratch_database(
    base_settings: Settings, db_settings: Settings
) -> None:
    cases, hashes = runner.load_cases(base_settings.ccp_golden_dir)
    chosen = runner.select(cases, level="workflow")
    report = runner.run(base_settings, chosen, hashes)
    failed = [(r.id, r.mismatches, r.error) for r in report.cases if not r.passed]
    assert failed == []
