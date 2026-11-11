"""MT8 (A-128): a rule change is an amendment by default and a correction by decision.

The defect: a rule's code or parameters changing took effect silently. The next evaluation
of each subject ran the new rule, and because a rule computes its own valid time (the
evidence's observation, a termination plus the SLA), its verdict superseded the subject's
belief from that past moment: the past was rewritten, and nothing recorded that a rule had
changed or who had decided the old one was wrong.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from types import ModuleType
from typing import Any

import pytest
from typer.testing import CliRunner

from ccp import cli
from ccp.compliance_state import writer
from ccp.config import Settings
from ccp.evaluators import rederive, rule_changes, rule_versions, runner
from ccp.evaluators import verdict as verdicts
from ccp.evaluators.deterministic import gh2, hr5
from ccp.evaluators.verdict import Verdict
from ccp.knowledge import catalogue
from ccp.observability import steps
from ccp.storage import bitemporal
from ccp.storage.scoped import ScopedDB
from ccp.types import VerdictStatus
from tests.support.pipeline import prepared_tenants

pytestmark = pytest.mark.db

GLOBEX_ORG = "GH-2|org:github:asset:org:globex-example"


@pytest.fixture
def world(base_settings: Settings) -> Iterator[tuple[Settings, dict[str, ScopedDB]]]:
    with prepared_tenants(base_settings, "ccp_rulechange") as (settings, dbs):
        for db in dbs.values():
            runner.enqueue_all(db, settings, tiers=(1,))
            runner.drain(db, settings)
            writer.drain(db)
        yield settings, dbs


def _new_rule(
    monkeypatch: pytest.MonkeyPatch,
    test_id: str,
    module: ModuleType,
    change: Callable[[Verdict], Verdict],
) -> None:
    """Ship a new version of one test's rule: a new rule stamp, and a new decision."""
    real_version = rule_versions.rule_version
    monkeypatch.setattr(
        rule_versions,
        "rule_version",
        lambda t: f"{t.lower()}-v2" if t == test_id else real_version(t),
    )
    real = module.evaluate
    monkeypatch.setattr(module, "evaluate", lambda ctx, subject: change(real(ctx, subject)))


def _passes(verdict: Verdict) -> Verdict:
    return verdict.model_copy(
        update={"status": VerdictStatus.passed, "reasoning": "the new rule passes it"}
    )


def _as_of(db: ScopedDB, subject: str, valid_at: datetime) -> bitemporal.StateRow:
    row = bitemporal.state_as_of(db, subject, valid_at, datetime.now(UTC))
    assert row is not None
    return row


def test_a_rule_change_is_an_amendment_and_the_past_stands(
    world: tuple[Settings, dict[str, ScopedDB]], monkeypatch: pytest.MonkeyPatch
) -> None:
    settings, dbs = world
    globex = dbs["globex"]
    before = bitemporal.current_state(globex, GLOBEX_ORG)
    assert before is not None and before.status is VerdictStatus.failed  # over its limit
    _new_rule(monkeypatch, "GH-2", gh2, _passes)
    runner.enqueue_all(globex, settings, tests=("GH-2",), tiers=(1,))  # the nightly full run
    runner.drain(globex, settings)
    writer.drain(globex)
    head = bitemporal.current_state(globex, GLOBEX_ORG)
    assert head is not None and (head.status, head.reason) == (VerdictStatus.passed, "rule_change")
    assert head.valid_from > before.valid_from  # from the new rule's first verdict, not the past
    then = _as_of(globex, GLOBEX_ORG, before.valid_from + timedelta(seconds=1))
    assert then.status is VerdictStatus.failed  # what was believed true then still stands
    (notice,) = globex.fetch_all("SELECT kind, to_stamp, decided_by FROM rule_changes")
    assert notice[0] == "amendment" and notice[1].startswith("gh-2-v2:") and notice[2] == "ccp"


def test_a_correction_re_decides_the_past_from_what_each_verdict_read(
    world: tuple[Settings, dict[str, ScopedDB]], monkeypatch: pytest.MonkeyPatch
) -> None:
    settings, dbs = world
    acme = dbs["acme"]
    subject = "HR-5|tenant"
    before = bitemporal.current_state(acme, subject)
    assert before is not None and before.status is VerdictStatus.failed
    _new_rule(monkeypatch, "HR-5", hr5, _passes)
    shown = rule_changes.preview(acme, settings, "HR-5")
    assert [(f.subject_key, f.before, f.after) for f in shown.subjects] == [
        (subject, "fail", "pass")
    ]
    with pytest.raises(rule_changes.SignOffRequired):  # FAIL to PASS needs a named person
        rule_changes.correct(acme, settings, "HR-5", "rules-owner")
    rule_changes.correct(acme, settings, "HR-5", "rules-owner", signed_off_by="audit-lead")
    writer.drain(acme)
    head = bitemporal.current_state(acme, subject)
    assert head is not None and (head.status, head.reason) == (VerdictStatus.passed, "correction")
    assert head.valid_from == before.valid_from  # the past itself is re-decided
    assert _as_of(acme, subject, before.valid_from + timedelta(seconds=1)).status.value == "pass"
    (row,) = acme.fetch_all("SELECT kind, signed_off_by, subjects FROM rule_changes")
    assert (row[0], row[1]) == ("correction", "audit-lead")
    assert row[2] == [{"subject_key": subject, "before": "fail", "after": "pass"}]
    (corrected,) = verdicts.latest_per_subject(acme, "HR-5")
    assert corrected.trigger_reason == "correction"
    assert rederive.rederive(acme, settings, corrected).outcome == "exact"
    with pytest.raises(LookupError):  # nothing left decided under the old rule
        rule_changes.correct(acme, settings, "HR-5", "rules-owner", signed_off_by="audit-lead")


def test_rows_and_manifests_name_the_catalogue_in_force(
    world: tuple[Settings, dict[str, ScopedDB]], monkeypatch: pytest.MonkeyPatch
) -> None:
    settings, dbs = world
    globex = dbs["globex"]
    rows = globex.fetch_all("SELECT DISTINCT catalogue_version FROM compliance_state")
    assert [r[0] for r in rows] == [catalogue.version()]
    (verdict,) = verdicts.latest_per_subject(globex, "GH-2")
    assert verdict.manifest is not None and verdict.manifest["catalogue"] == catalogue.version()

    def scratch() -> Settings:
        steps.configure(settings, sink=None)
        return settings

    monkeypatch.setattr(cli, "_settings", scratch)
    _new_rule(monkeypatch, "GH-2", gh2, _passes)
    shown = CliRunner().invoke(cli.app, ["rules", "preview", "GH-2", "--tenant", "globex"])
    assert shown.exit_code == 0, shown.output
    assert f"* {GLOBEX_ORG}: fail -> pass" in shown.output
    refused: Any = CliRunner().invoke(
        cli.app, ["rules", "correct", "GH-2", "--by", "rules-owner", "--tenant", "globex"]
    )
    assert refused.exit_code == 2 and "sign-off" in refused.output
