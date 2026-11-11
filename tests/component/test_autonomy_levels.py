"""LT8, narrowed to review (A-171): a draft skips a person's review only in a reversible,
low-impact class an administrator promoted on a published snapshot measured on the bundle
in force; a seeded sample and the exposure budget still go to a person, every skipped
review cites its snapshot, and a bundle change or a failing quality check demotes at once.

The defect: every draft needed a person whatever its record, and people approve almost
everything put in front of them, so blanket review added little and nothing measured it.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from ulid import ULID

from ccp.compliance_state import autonomy
from ccp.config import Settings
from ccp.llm import release
from ccp.storage.scoped import ScopedDB
from tests.support.pipeline import prepared_tenants

pytestmark = pytest.mark.db


def _snapshot(folder: Path, workflow: str, bundle: str, **kw: object) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    body = {
        "workflow": workflow,
        "bundle_id": bundle,
        "false_pass_upper": 0.0093,
        "withheld": None,
        "fail_labelled": 320,
        "pass_k": 0.95,
        **kw,
    }
    (folder / f"{workflow}-{bundle}.json").write_text(json.dumps(body), encoding="utf-8")


@pytest.fixture(scope="module")
def world(base_settings: Settings) -> Iterator[tuple[Settings, ScopedDB]]:
    with prepared_tenants(base_settings, "ccp_autonomy", ("acme",)) as (settings, dbs):
        yield settings, dbs["acme"]


def test_a_class_skips_review_only_on_a_snapshot_that_meets_the_bar(
    world: tuple[Settings, ScopedDB], tmp_path: Path
) -> None:
    settings, acme = world
    bundle = release.bundle_id(settings)
    published = tmp_path / "published"
    with pytest.raises(ValueError, match="reversible, low-impact"):
        autonomy.set_level(
            acme, settings, "questionnaire", "drafted_answer", "sampled", "ada", "try", published
        )
    with pytest.raises(ValueError, match="no published quality snapshot"):
        autonomy.set_level(
            acme, settings, "questionnaire", "library_answer", "sampled", "ada", "try", published
        )
    _snapshot(published / "2026-Q3", "questionnaire", "old-bundle")
    with pytest.raises(ValueError, match="measured bundle old-bundle"):
        autonomy.set_level(
            acme, settings, "questionnaire", "library_answer", "sampled", "ada", "try", published
        )
    _snapshot(
        published / "2026-Q4",
        "questionnaire",
        bundle,
        false_pass_upper=None,
        withheld="40 cases",
        fail_labelled=40,
    )
    with pytest.raises(ValueError, match="withholds its bound"):
        autonomy.set_level(
            acme, settings, "questionnaire", "library_answer", "sampled", "ada", "try", published
        )
    _snapshot(published / "2026-Q4", "questionnaire", bundle)
    level_id = autonomy.set_level(
        acme,
        settings,
        "questionnaire",
        "library_answer",
        "sampled",
        "ada",
        "the record holds",
        published,
    )
    found = autonomy.current(acme, "questionnaire", "library_answer")
    assert found.level_id == level_id and found.level == "sampled" and found.bundle_id == bundle

    outcomes = [
        autonomy.decide(acme, "questionnaire", "library_answer", str(ULID())) for _ in range(60)
    ]
    assert outcomes.count("sampled_for_review") >= 1  # a seeded sample still reaches a person
    assert outcomes.count("over_budget") >= 1 and outcomes.count("auto") == 50  # the day's budget
    cited = acme.fetch_one("SELECT snapshot FROM auto_decisions WHERE outcome = 'auto' LIMIT 1")
    assert (
        cited is not None
        and (json.loads(cited[0]) if isinstance(cited[0], str) else cited[0])["bundle_id"] == bundle
    )
    assert autonomy.decide(acme, "questionnaire", "drafted_answer", "x") == "review"

    assert autonomy.watch(acme, settings) == []  # nothing changed
    acme.execute(
        "INSERT INTO quality_checks (tenant_id, check_id, kind, bundle_id, passed, measured)"
        " VALUES ('acme', %s, 'relabel', %s, false, '{}')",
        (str(ULID()), bundle),
    )
    (demoted,) = autonomy.watch(acme, settings)
    assert "quality check relabel failed" in demoted
    assert autonomy.current(acme, "questionnaire", "library_answer").level == "approve_each"
    assert autonomy.decide(acme, "questionnaire", "library_answer", "y") == "review"


def test_the_simulator_counts_what_would_have_skipped_review() -> None:
    day = datetime(2026, 10, 1, 9, tzinfo=UTC)
    history: list[tuple[str, datetime, Any]] = [
        (f"a{i}", day + timedelta(minutes=i), "accepted" if i % 10 else "changed")
        for i in range(70)
    ]
    result = autonomy.simulate("acme", "questionnaire", "library_answer", history)
    assert result.items == 70 and result.auto == 50
    assert result.auto + result.sampled_for_review + result.over_budget == 70
    assert 0 < result.false_accepts <= 7  # the changed drafts that would have gone out unread
