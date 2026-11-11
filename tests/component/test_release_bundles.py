"""MT7 (A-142): every verdict and agent run names the release bundle it ran under.

The defect: a verdict recorded its model and prompt version, but not the whole of what
shaped it (the other roles' models, their thinking settings, the tool texts, the rule
catalogue, the configuration), so a change could ship without anything saying which results
it touched, and an eval baseline could not say what it measured.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime

import pytest

from ccp.agent import threads
from ccp.config import Settings
from ccp.evaluators import runner as evaluations
from ccp.llm import release
from ccp.storage.scoped import ScopedDB
from tests.support.pipeline import prepared_tenants

NOW = datetime(2026, 9, 11, 12, tzinfo=UTC)


def test_a_bundle_is_stable_and_any_change_is_a_new_one() -> None:
    settings = Settings()
    first = release.bundle_id(settings)
    assert release.bundle_id(Settings()) == first and len(first) == 16
    manifest = release.manifest_of(first)
    assert manifest is not None
    assert manifest["models"] == release.roles(settings)
    defaults = Settings.model_fields
    assert defaults["ccp_tier2_model"].default == "claude-haiku-5-5"  # thinking off (A-100)
    assert defaults["ccp_tier3_model"].default == "claude-sonnet-5-5"  # the agents (A-142)
    assert manifest["model_behaviour"]["claude-haiku-5-5"]["thinking_off"] == "disabled"
    assert set(manifest) == {"models", "model_behaviour", "prompts_and_tools", "rules", "config"}
    judged = release.bundle_id(settings.model_copy(update={"ccp_judge_model": "claude-opus-5-5"}))
    assert judged != first
    changed = release.differences(manifest, release.manifest_of(judged) or {})
    assert "models.judge: 'claude-opus-5' -> 'claude-opus-5-5'" in changed


@pytest.fixture(scope="module")
def world(base_settings: Settings) -> Iterator[tuple[Settings, ScopedDB]]:
    with prepared_tenants(base_settings, "ccp_bundles", ("acme",)) as (settings, dbs):
        yield settings, dbs["acme"]


@pytest.mark.db
def test_every_verdict_and_run_names_its_bundle(world: tuple[Settings, ScopedDB]) -> None:
    settings, acme = world
    evaluations.enqueue_all(acme, settings, now=NOW, tests=("HR-1", "GH-2"))
    evaluations.drain(acme, settings, now=NOW)
    expected = release.bundle_id(settings)
    rows = acme.fetch_all("SELECT DISTINCT bundle_id FROM verdicts")
    assert rows == [(expected,)]  # every verdict, none without
    kept = acme.fetch_one("SELECT manifest FROM release_bundles WHERE bundle_id = %s", (expected,))
    assert kept is not None and kept[0]["models"] == release.roles(settings)
    threads.create(acme, "acme:investigator:bundle-test", "investigator", "HR-1", "HR-1|x")
    run = acme.fetch_one(
        "SELECT bundle_id FROM agent_threads WHERE thread_id = 'acme:investigator:bundle-test'"
    )
    assert run == (release.bundle_id(Settings()),)
