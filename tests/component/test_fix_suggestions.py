"""LT1 (A-159): fixes are suggested, never applied. Code renders each one from a versioned
template, with parameters checked against their patterns, and checks the result offline
(commands, Terraform syntax and scope) before anyone sees it; when the rule explains the
failure, code writes the investigation itself and needs no model.

The defects: a cloud finding had no fix path at all, a model-written command or diff could
carry anything, and an AWS failure parked for want of a model key although the rule already
said exactly what was wrong.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import boto3
import psycopg
import pytest

from ccp.config import Settings
from ccp.connectors.aws.connector import BLOCKS
from ccp.evaluators.verdict import latest_per_subject
from ccp.investigation import runner as investigations
from ccp.investigation import schema
from ccp.knowledge import graph as knowledge
from ccp.lookups import runner as lookups
from ccp.remediation import confirm, fixes, suggestions
from ccp.storage.scoped import ScopedDB
from tests.support.aws_lookups import (
    IAM_TF,
    NOW,
    TF,
    TRAIL_TF,
    RecordedAws,
    RecordedGitHub,
    failing_account,
    pull_and_evaluate,
)


def _file(path: str, text: str) -> dict[str, str]:
    return {"repo": "acme/infra", "path": path, "sha": "abc", "text": text}


# -- rendering, offline ----------------------------------------------------------------------


def test_every_fix_names_known_tests_and_versions_its_templates() -> None:
    known = set(knowledge.load().tests)
    catalogue = fixes.load()
    assert set(catalogue) == {
        "s3_block_public_access",
        "iam_user_remove_console_access",
        "iam_user_enable_mfa",
        "cloudtrail_multi_region",
        "github_remove_collaborator",
    }
    for fix in catalogue.values():
        assert fix.tests and fix.cli and fix.console and fix.verify
        assert len(fix.sha256) == 64
        assert set(fix.tests) <= known


def test_a_bucket_fix_adds_one_block_after_the_bucket_and_passes_its_checks() -> None:
    fix = fixes.get("s3_block_public_access")
    rendered = fixes.render(fix, {"bucket": "ccp-open"}, [_file("s3.tf", TF)])
    assert rendered.passed, rendered.failed
    change = rendered.terraform
    assert change is not None and change["address"] == "aws_s3_bucket.open"
    assert '+resource "aws_s3_bucket_public_access_block" "open" {' in change["diff"]
    assert "+  bucket = aws_s3_bucket.open.id" in change["diff"]
    assert not any(
        line.startswith("-") and not line.startswith("---") for line in change["diff"].splitlines()
    )
    assert rendered.cli[0].startswith("aws s3api put-public-access-block --bucket ccp-open ")
    assert "ccp-open" in rendered.console[0]
    again = fixes.render(fix, {"bucket": "ccp-open"}, [_file("s3.tf", TF)])
    assert again.payload_sha256() == rendered.payload_sha256()  # the same rendering


def test_parameters_outside_their_pattern_are_refused_before_rendering() -> None:
    fix = fixes.get("s3_block_public_access")
    for bad in ("ccp-open; rm -rf /", "$(whoami)", "CCP-OPEN", "a"):
        with pytest.raises(fixes.Refused, match="does not match"):
            fixes.render(fix, {"bucket": bad})
    with pytest.raises(fixes.Refused, match="takes"):
        fixes.render(fix, {"bucket": "ccp-open", "extra": "x"})
    with pytest.raises(fixes.Refused, match="no fix"):
        fixes.get("delete_everything")


def test_removing_a_login_profile_and_setting_a_trail_attribute_stay_in_scope() -> None:
    removed = fixes.render(
        fixes.get("iam_user_remove_console_access"),
        {"user_name": "alice"},
        [_file("iam.tf", IAM_TF)],
    )
    assert removed.passed, removed.failed
    assert removed.terraform is not None
    assert '-resource "aws_iam_user_login_profile" "alice" {' in removed.terraform["diff"]
    assert '-resource "aws_iam_user" "alice" {' not in removed.terraform["diff"]

    trail = fixes.render(
        fixes.get("cloudtrail_multi_region"), {"trail_name": "org"}, [_file("trail.tf", TRAIL_TF)]
    )
    assert trail.passed, trail.failed
    assert trail.terraform is not None
    assert "-  is_multi_region_trail = false" in trail.terraform["diff"]
    assert "+  is_multi_region_trail = true" in trail.terraform["diff"]
    missing = TRAIL_TF.replace("  is_multi_region_trail = false\n", "")
    added = fixes.render(
        fixes.get("cloudtrail_multi_region"), {"trail_name": "org"}, [_file("trail.tf", missing)]
    )
    assert added.passed and "+  is_multi_region_trail = true" in (added.terraform or {})["diff"]


def test_a_template_that_reaches_past_its_resource_fails_the_scope_check() -> None:
    fix = fixes.get("s3_block_public_access")
    assert fix.terraform is not None
    greedy = fixes.Fix(
        **{
            **fix.__dict__,
            "terraform": fixes.TerraformSpec(
                "add_block",
                "aws_s3_bucket",
                'resource "aws_iam_user" "{name}" {\n  name = "backdoor"\n}',
            ),
        }
    )
    rendered = fixes.render(greedy, {"bucket": "ccp-open"}, [_file("s3.tf", TF)])
    assert not rendered.passed and [c.name for c in rendered.failed] == ["scope"]
    smuggled = fixes.Fix(
        **{**fix.__dict__, "cli": ("aws s3api delete-bucket --bucket {bucket}; curl x",)}
    )
    assert [c.name for c in fixes.render(smuggled, {"bucket": "ccp-open"}).failed] == ["cli"]
    swapped = fixes.Fix(**{**fix.__dict__, "cli": ("{bucket} s3api put-bucket-acl",)})
    assert fixes.render(swapped, {"bucket": "ccp-open"}).cli  # no fixed prefix: nothing to hold
    unbalanced = fixes.Fix(
        **{
            **fix.__dict__,
            "terraform": fixes.TerraformSpec(
                "add_block",
                "aws_s3_bucket",
                'resource "aws_s3_bucket_public_access_block" "{name}" {\n'
                "  bucket = {address}.id\n",
            ),
        }
    )
    failed = {
        c.name
        for c in fixes.render(unbalanced, {"bucket": "ccp-open"}, [_file("s3.tf", TF)]).failed
    }
    assert "syntax" in failed


def test_no_terraform_change_when_the_resource_is_not_in_code_or_named_twice() -> None:
    fix = fixes.get("s3_block_public_access")
    assert fixes.render(fix, {"bucket": "ccp-open"}, []).terraform is None
    twice = [_file("a.tf", TF), _file("b.tf", TF)]
    rendered = fixes.render(fix, {"bucket": "ccp-open"}, twice)
    assert rendered.terraform is None and rendered.passed  # commands and steps still offered


# -- end to end ------------------------------------------------------------------------------


@pytest.fixture
def world(base_settings: Settings, tmp_path: Path) -> Iterator[tuple[Settings, ScopedDB]]:
    try:
        with failing_account(
            base_settings, tmp_path, "ccp_fixes", ("AWS-1", "AWS-2", "AWS-3")
        ) as w:
            yield w
    except psycopg.OperationalError as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"Postgres not reachable: {exc}")


@pytest.mark.db
def test_code_writes_the_investigation_and_offers_checked_fixes_without_a_model(
    world: tuple[Settings, ScopedDB],
) -> None:
    settings, acme = world
    failed = {v.test_id: v for v in latest_per_subject(acme) if v.status.value == "fail"}
    assert set(failed) == {"AWS-1", "AWS-2", "AWS-3"}
    connected = {"aws": RecordedAws(), "github": RecordedGitHub()}
    lookups.drain(acme, settings, connected.get, NOW)
    summary = investigations.drain(acme, settings)  # no model key in this environment
    assert summary.written_by_code == 3 and summary.parked == 0

    bucket = schema.for_verdict(acme, failed["AWS-1"].verdict_id)
    assert bucket is not None and bucket.model is None and bucket.prompt_version == "code:fixes_v1"
    assert "last changed by ops-bot" in bucket.root_cause
    assert "defined in code at acme/infra/s3.tf" in bucket.root_cause
    (offered,) = suggestions.for_verdict(acme, failed["AWS-1"].verdict_id)
    assert offered.fix == "s3_block_public_access" and offered.status == "open"
    assert offered.source == "code" and offered.resource == "ccp-open"
    assert offered.body["terraform"]["path"] == "s3.tf" and offered.lookup_ids
    assert {c["name"] for c in offered.checks} >= {"params", "cli", "syntax", "scope"}

    iam = {s.fix: s for s in suggestions.for_verdict(acme, failed["AWS-2"].verdict_id)}
    assert set(iam) == {"iam_user_remove_console_access", "iam_user_enable_mfa"}
    assert iam["iam_user_remove_console_access"].body["terraform"]["kind"] == "remove_block"
    assert iam["iam_user_enable_mfa"].body["terraform"] is None  # MFA is enrolled by a person

    (trail,) = suggestions.for_verdict(acme, failed["AWS-3"].verdict_id)
    assert trail.params == {"trail_name": "org"}
    assert "+  is_multi_region_trail = true" in trail.body["terraform"]["diff"]

    # the same rendering for the same resource is the same suggestion; a new one supersedes it
    rendered = fixes.render(fixes.get("s3_block_public_access"), {"bucket": "ccp-open"})
    same_id, new = suggestions.suggest(
        acme,
        verdict_id=failed["AWS-1"].verdict_id,
        test_id="AWS-1",
        resource="ccp-open",
        source="code",
        rendered=fixes.render(
            fixes.get("s3_block_public_access"),
            {"bucket": "ccp-open"},
            [_file("s3.tf", TF)],
        ),
    )
    assert not new and same_id == offered.suggestion_id
    newer, new = suggestions.suggest(
        acme,
        verdict_id=failed["AWS-1"].verdict_id,
        test_id="AWS-1",
        resource="ccp-open",
        source="code",
        rendered=rendered,  # no Terraform this time
    )
    assert new and newer != offered.suggestion_id
    old = suggestions.get(acme, offered.suggestion_id)
    assert old is not None and old.status == "superseded"
    assert [s.suggestion_id for s in suggestions.live_for(acme, "AWS-1", "ccp-open")] == [newer]


@pytest.mark.db
def test_only_a_later_read_confirms_a_fix_and_a_relapse_reopens_it(
    world: tuple[Settings, ScopedDB],
) -> None:
    settings, acme = world
    tests = ("AWS-1", "AWS-2", "AWS-3")
    connected = {"aws": RecordedAws(), "github": RecordedGitHub()}
    lookups.drain(acme, settings, connected.get, NOW)
    investigations.drain(acme, settings)
    (bucket,) = suggestions.live_for(acme, "AWS-1", "ccp-open")
    confirm.decide(acme, bucket.suggestion_id, "accepted", "alex")
    with pytest.raises(ValueError, match="says why"):
        confirm.decide(acme, bucket.suggestion_id, "dismissed", "alex")
    with pytest.raises(ValueError, match="a person records"):
        confirm.decide(acme, bucket.suggestion_id, "confirmed", "alex")  # type: ignore[arg-type]
    assert confirm.drain(acme).confirmed == 0  # accepting is not fixing
    found = suggestions.get(acme, bucket.suggestion_id)
    assert found is not None and found.status == "accepted"

    # the person applies the fix in AWS; CCP's next pull sees it
    s3 = boto3.client("s3", region_name="us-east-1")
    s3.put_public_access_block(
        Bucket="ccp-open", PublicAccessBlockConfiguration=dict.fromkeys(BLOCKS, True)
    )
    later = datetime.now(UTC) + timedelta(minutes=1)
    pull_and_evaluate(settings, acme, later, tests)
    done = confirm.drain(acme)
    assert done.confirmed == 1
    fixed = suggestions.get(acme, bucket.suggestion_id)
    assert fixed is not None and fixed.status == "confirmed" and fixed.status_verdict_id
    assert all(s.status == "open" for s in suggestions.live_for(acme, "AWS-2", "alice"))

    # someone removes the block again within 30 days: the fix reopens
    s3.delete_public_access_block(Bucket="ccp-open")
    pull_and_evaluate(settings, acme, later + timedelta(minutes=1), tests)
    assert confirm.drain(acme).reopened == 1
    relapsed = suggestions.get(acme, bucket.suggestion_id)
    assert relapsed is not None and relapsed.status == "reopened"
    m = confirm.metrics(acme)
    assert m.confirmed == 1 and m.reopened == 1 and m.reopen_rate == 1.0
    assert m.median_hours_to_confirmed is not None and m.median_hours_to_confirmed > 0
