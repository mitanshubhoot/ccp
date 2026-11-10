"""MT1 (A-122): the connector conformance harness, its negative controls and its safety check.

The defect: nothing tested a connector against what the sync contract promises. Each
promise (a hidden listing is never complete, absence only from a whole listing, a stop names
its kind, an unchanged re-pull is the same events) had a test or two of its own, and a new
connector, or a change to the GitHub client, could break one with nothing to say so.
"""

from __future__ import annotations

from typer.testing import CliRunner

from ccp import cli
from ccp.connectors import conformance
from ccp.connectors.github import conformance as github


def test_every_live_connector_passes_its_suite() -> None:
    suites, _ = conformance.default()
    report = conformance.run(suites, [])
    failing = [(r.suite, r.scenario, r.problems) for r in report.results if not r.passed]
    assert failing == []
    assert {r.suite for r in report.results} == {"github", "manual"}
    assert len([r for r in report.results if r.suite == "github"]) == 14  # 13 and the check


def test_every_negative_control_is_caught() -> None:
    _, controls = conformance.default()
    report = conformance.run([], controls)
    assert report.blind == []
    caught = {
        control.name: sorted(r.scenario for r in results if not r.passed)
        for control, results in report.controls
    }
    hidden_as_empty = caught["github: hidden listing read as empty"]
    assert "member-listing-hidden" in hidden_as_empty  # it would revoke every member
    assert "collaborators-hidden-on-one-repository" in hidden_as_empty
    assert "member-listing-hidden" in caught["github: complete over a gap"]
    assert caught["github: ids from the pull time"] == ["empty-organization", "normal"]
    assert "path-outside-the-folder" in caught["uploads: left-out entries ignored"]


def test_a_control_that_breaks_nothing_makes_the_run_blind() -> None:
    unbroken = conformance.Control("not broken", "nothing", github.Suite())
    report = conformance.run([], [unbroken])
    assert report.blind == ["not broken"] and not report.green


def test_the_safety_check_finds_a_planted_secret() -> None:
    assert conformance.unsafe("token ghp_" + "a" * 36)
    assert conformance.unsafe("github_pat_" + "b" * 30)
    assert conformance.unsafe('{"Authorization": "token x"}')
    assert conformance.unsafe("Bearer abc.def.ghi")
    assert conformance.unsafe("AKIA" + "C" * 16)
    assert not conformance.unsafe('{"login": "member-1", "node_id": "U_member-1"}')


def test_the_command_is_green() -> None:
    result = CliRunner().invoke(cli.app, ["connectors", "conformance"])
    assert result.exit_code == 0, result.output
    assert "FAIL" not in result.output and "BLIND" not in result.output
    assert "control github: hidden listing read as empty: caught by" in result.output
