"""LT1 (A-158): before anyone investigates a failing resource, code reads its AWS Config
history in 7-day windows, its CloudTrail events at under 2 requests a second, and the
Terraform that names it; every response lands as raw evidence before its typed row.

The defects: the Investigator saw only CCP's own snapshot of a failing setting, so it could
not say who changed it, when, or whether it lives in code; and handing a model live read
tools would put the connector's credentials in the process that reads untrusted text.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path
from typing import Any

import psycopg
import pytest

from ccp.agent import tools
from ccp.config import Settings
from ccp.connectors.aws.connector import AwsConnector, windows
from ccp.connectors.rate import RateBucket
from ccp.evaluators.verdict import latest_per_subject
from ccp.investigation import runner as investigations
from ccp.lookups import runner as lookups
from ccp.lookups import store, terraform
from ccp.retrieval.config import RetrievalConfig
from ccp.retrieval.embeddings import HashingEmbeddingClient
from ccp.storage.scoped import ScopedDB
from tests.support.aws_lookups import (
    ACCOUNT,
    NOW,
    TF,
    RecordedAws,
    RecordedGitHub,
    failing_account,
)

# -- the reads, without a database --------------------------------------------------------


def test_config_history_reads_seven_day_windows_and_pages_within_each() -> None:
    cut = windows(NOW - timedelta(days=30), NOW)
    assert len(cut) == 5 and cut[-1] == (NOW - timedelta(days=2), NOW)
    assert all(b - a <= timedelta(days=7) for a, b in cut)

    calls: list[dict[str, Any]] = []

    class _Config:
        def get_resource_config_history(self, **kw: Any) -> dict[str, Any]:
            calls.append(kw)
            if "nextToken" in kw or kw["earlierTime"] != cut[0][0]:
                return {"configurationItems": [{"configurationStateId": len(calls)}]}
            return {"configurationItems": [{"configurationStateId": 0}], "nextToken": "t"}

    class _Sts:
        def assume_role(self, **kw: Any) -> dict[str, Any]:
            return {"Credentials": {}}

    def client(creds: Any, service: str) -> Any:
        return _Sts() if service == "sts" else _Config()

    connector = AwsConnector(ACCOUNT, "arn:aws:iam::1:role/r", "x", client)
    items = connector.config_history("AWS::S3::Bucket", "ccp-open", NOW - timedelta(days=30), NOW)
    assert len(calls) == 6 and len(items) == 6  # five windows, the first one two pages
    assert {c["resourceId"] for c in calls} == {"ccp-open"}


def test_cloudtrail_pages_wait_their_turn_at_two_a_second() -> None:
    clock = [0.0]
    slept: list[float] = []

    def sleep(seconds: float) -> None:
        slept.append(seconds)
        clock[0] += seconds

    bucket = RateBucket(lookups.TRAIL_RATE, 1, clock=lambda: clock[0], sleep=sleep)

    class _Trail:
        def lookup_events(self, **kw: Any) -> dict[str, Any]:
            assert kw["LookupAttributes"] == [
                {"AttributeKey": "ResourceName", "AttributeValue": "b"}
            ]
            page = len(slept)
            more = {"NextToken": "n"} if page < 2 else {}
            return {"Events": [{"EventName": "PutBucketPolicy", "EventId": str(page)}], **more}

    class _Sts:
        def assume_role(self, **kw: Any) -> dict[str, Any]:
            return {"Credentials": {}}

    connector = AwsConnector(
        ACCOUNT, "r", "x", lambda creds, svc: _Sts() if svc == "sts" else _Trail()
    )
    events = connector.trail_events("ResourceName", "b", NOW - timedelta(days=1), NOW, bucket)
    assert len(events) == 3
    assert slept == [0.5, 0.5]  # the second and third pages each waited half a second


def test_the_terraform_scanner_finds_the_block_that_names_a_resource() -> None:
    found = terraform.blocks(TF)
    assert [(b.address, b.start_line, b.end_line) for b in found] == [
        ("aws_s3_bucket.open", 2, 7),
        ("aws_s3_bucket.blocked", 9, 14),  # the heredoc's braces are text, not structure
    ]
    (named,) = terraform.mentioning(TF, "ccp-open", ("aws_s3_bucket",))
    assert named.address == "aws_s3_bucket.open"
    assert terraform.mentioning(TF, "ccp-open", ("aws_iam_user",)) == []
    assert terraform.balanced(TF)
    assert not terraform.balanced(TF + '\nresource "x" "y" {\n')
    assert not terraform.balanced('bucket = "unterminated\n')


# -- end to end ------------------------------------------------------------------------------


@pytest.fixture
def world(base_settings: Settings, tmp_path: Path) -> Iterator[tuple[Settings, ScopedDB]]:
    try:
        with failing_account(base_settings, tmp_path, "ccp_lookups", ("AWS-1", "AWS-2")) as w:
            yield w
    except psycopg.OperationalError as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"Postgres not reachable: {exc}")


@pytest.mark.db
def test_failing_resources_are_looked_up_landed_and_then_investigated(
    world: tuple[Settings, ScopedDB],
) -> None:
    settings, acme = world
    failed = {v.test_id: v for v in latest_per_subject(acme) if v.status.value == "fail"}
    assert set(failed) == {"AWS-1", "AWS-2"}
    assert store.failing_of(failed["AWS-1"]) == [
        {"entity_id": f"aws:asset:s3:{ACCOUNT}:ccp-open", "name": "ccp-open"}
    ]
    queued = investigations.InvestigationsConsumer(acme, settings)
    while queued.run_once():
        pass
    assert queued.summary.enqueued == 0  # their lookups come first

    aws, github = RecordedAws(), RecordedGitHub()
    connected = {"aws": aws, "github": github}
    summary = lookups.drain(acme, settings, connected.get, NOW)
    assert summary.enqueued == 2 and summary.investigations == 2
    rows = store.for_verdict(acme, failed["AWS-1"].verdict_id)
    by_kind = {r.kind: r for r in rows}
    assert set(by_kind) == {"config_history", "cloudtrail", "infra_code"}
    assert {r.resource for r in rows} == {"ccp-open"}
    history = by_kind["config_history"].result["states"]
    assert history[1]["changed"] == ["PublicAccessBlockConfiguration"]
    trail = by_kind["cloudtrail"].result["events"]
    assert [e["event_name"] for e in trail] == ["DeleteBucketPublicAccessBlock"]  # reads dropped
    (file,) = by_kind["infra_code"].result["files"]
    assert file["resources"][0]["address"] == "aws_s3_bucket.open" and "text" in file
    for row in rows:  # each landed its responses before the row was written
        assert row.response_ids
        landed = acme.fetch_all(
            "SELECT sync_run_id FROM raw_responses WHERE response_id = ANY(%s)",
            (row.response_ids,),
        )
        assert {r[0] for r in landed} == {f"lookup:{failed['AWS-1'].verdict_id}"}

    iam = {r.kind: r for r in store.for_verdict(acme, failed["AWS-2"].verdict_id)}
    assert iam["config_history"].params["resource_id"] != "alice"  # the immutable user id
    assert iam["infra_code"].result["files"][0]["resources"][0]["address"] == "aws_iam_user.alice"

    jobs = acme.fetch_all("SELECT ref FROM event_log WHERE topic = 'investigations' ORDER BY ref")
    assert sorted(r[0] for r in jobs) == sorted(v.verdict_id for v in failed.values())

    ctx = tools.ToolContext(acme, HashingEmbeddingClient(), None, RetrievalConfig())
    seen = tools.get_lookups(ctx, tools.GetLookupsArgs(verdict_id=failed["AWS-1"].verdict_id))
    tools.LookupsOut.model_validate(seen)
    code = next(lk for lk in seen["lookups"] if lk["kind"] == "infra_code")
    assert "text" not in code["result"]["files"][0]  # the model never reads file text

    again = lookups.drain(acme, settings, connected.get, NOW)
    assert again.enqueued == 0 and again.looked_up == 0 and len(aws.calls) == 4


@pytest.mark.db
def test_without_a_connection_a_lookup_is_recorded_as_skipped(
    world: tuple[Settings, ScopedDB],
) -> None:
    settings, acme = world
    summary = lookups.drain(acme, settings, lambda name: None, NOW)
    assert summary.investigations == 2  # the investigation is queued all the same
    statuses = acme.fetch_all("SELECT DISTINCT status, error FROM lookups")
    assert statuses == [("skipped", "no live connection")]
