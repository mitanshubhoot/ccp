"""Pure pieces of the eval runner: suite selection, the unit-row footer parser, and the
offset-based usage reader that replaces whole-log scans."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ccp.config import Settings
from ccp.evals import metrics, runner


def test_all_cases_selects_suites_and_hashes_the_adversarial_file(base_settings: Settings) -> None:
    golden, hashes = runner.all_cases(base_settings, suite="golden")
    assert golden and "adversarial/cases.yaml" not in hashes
    adversarial, hashes = runner.all_cases(base_settings, suite="adversarial")
    assert adversarial and all(c.target == "adversarial_tier2" for c in adversarial)
    assert "adversarial/cases.yaml" in hashes
    both, _ = runner.all_cases(base_settings, suite="all")
    assert len(both) == len(golden) + len(adversarial)
    with pytest.raises(ValueError):
        runner.all_cases(base_settings, suite="nope")


@pytest.mark.parametrize(
    ("footer", "returncode", "passed", "total"),
    [
        ("12 passed in 1.0s", 0, 12, 12),
        ("1 failed, 11 passed in 2.0s", 1, 11, 12),
        ("2 errors, 3 passed in 0.5s", 1, 3, 5),
        ("1 error, 3 passed in 0.5s", 1, 3, 4),
        ("5 passed, 1 xfailed in 0.1s", 0, 5, 5),
        ("", 2, 0, 1),  # a collection crash: never 0/0
    ],
)
def test_pytest_footer_parsing(footer: str, returncode: int, passed: int, total: int) -> None:
    row = runner.parse_pytest_summary(footer, returncode)
    assert (row.passed, row.total) == (passed, total)


def test_usage_since_reads_only_the_bytes_appended_after_the_offset(tmp_path: Path) -> None:
    log = tmp_path / "steps.jsonl"
    earlier = {
        "gen_ai.request.model": "claude-haiku-4-5",
        "gen_ai.usage.input_tokens": 1000,
        "gen_ai.usage.output_tokens": 0,
    }
    log.write_text(json.dumps(earlier) + "\n", encoding="utf-8")
    offset = log.stat().st_size
    later = {**earlier, "ccp.cost_usd": 0.25}
    with log.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(later) + "\nnot json\n")
    tokens, usd = metrics.usage_since(log, offset)
    assert tokens == 1000 and usd == 0.25  # the recorded cost wins over the estimate
    assert metrics.usage_since(log, 0)[0] == 2000
    assert metrics.usage_since(tmp_path / "missing.jsonl", 0) == (0, 0.0)
