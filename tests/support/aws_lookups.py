"""A moto AWS account with failing resources, and recorded lookup surfaces (A-158, A-159)."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import boto3
from moto import mock_aws

from ccp.config import Settings
from ccp.connectors.aws.connector import AwsConnector
from ccp.connectors.base import Received
from ccp.evaluators import runner as evaluations
from ccp.ingest.landing import sync_connector
from ccp.ingest.raw_zone import RawZone
from ccp.normalize.consumer import normalize_all
from ccp.storage.db import app_pool, scratch_database
from ccp.storage.scoped import ScopedDB
from ccp.tenancy import TenantContext

ACCOUNT = "123456789012"
NOW = datetime(2026, 9, 11, 12, tzinfo=UTC)
TF = """\
# storage
resource "aws_s3_bucket" "open" {
  bucket = "ccp-open"
  tags = {
    team = "data" # owner
  }
}

resource "aws_s3_bucket" "blocked" {
  bucket = "ccp-blocked"
  policy = <<EOF
{ "Version": "2012-10-17" }
EOF
}
"""

IAM_TF = """resource "aws_iam_user" "alice" {
  name = "alice"
}

resource "aws_iam_user_login_profile" "alice" {
  user = aws_iam_user.alice.name
}
"""
TRAIL_TF = """resource "aws_cloudtrail" "org" {
  name           = "org"
  s3_bucket_name = "ccp-trail-logs"
  is_multi_region_trail = false
}
"""
FILES = {
    "ccp-open": ("s3.tf", TF),
    "alice": ("iam.tf", IAM_TF),
    "aws_cloudtrail": ("trail.tf", TRAIL_TF),
}


class RecordedAws:
    """The AWS connector's lookup surface, answering from recorded responses."""

    name = "aws"
    account_id = ACCOUNT

    def __init__(self) -> None:
        self.responses: list[Received] = []
        self.calls: list[tuple[str, str]] = []

    def _keep(self, what: str) -> None:
        self.responses.append(Received("POST", what, 200, f"req-{len(self.calls)}", b"{}", NOW))

    def config_history(
        self, resource_type: str, resource_id: str, start: datetime, end: datetime
    ) -> list[dict[str, Any]]:
        self.responses = []
        self.calls.append(("config", resource_id))
        self._keep("config:GetResourceConfigHistory")
        return [
            {
                "configurationItemCaptureTime": NOW - timedelta(days=3),
                "configurationItemStatus": "OK",
                "configurationStateId": "1",
                "supplementaryConfiguration": {"PublicAccessBlockConfiguration": {"a": True}},
            },
            {
                "configurationItemCaptureTime": NOW - timedelta(days=1),
                "configurationItemStatus": "OK",
                "configurationStateId": "2",
                "supplementaryConfiguration": {"PublicAccessBlockConfiguration": {"a": False}},
            },
        ]

    def trail_events(
        self, key: str, value: str, start: datetime, end: datetime, bucket: Any = None
    ) -> list[dict[str, Any]]:
        self.responses = []
        self.calls.append(("trail", value))
        self._keep("cloudtrail:LookupEvents")
        return [
            {"EventName": "DeleteBucketPublicAccessBlock", "EventId": "e1", "Username": "ops-bot"},
            {"EventName": "GetBucketAcl", "EventId": "e2", "Username": "auditor"},
        ]


class RecordedGitHub:
    name = "github"

    def __init__(self) -> None:
        self.responses: list[Received] = []

    def search_code(self, tenant: TenantContext, term: str) -> list[dict[str, Any]]:
        self.responses = [Received("GET", "/search/code", 200, "gh-1", b"{}", NOW)]
        if term not in FILES:
            return []
        path, text = FILES[term]
        return [{"repo": "acme/infra", "path": path, "sha": "abc", "text": text}]


def aws_world() -> None:
    """Two buckets (one open), a console user without MFA, and a single-region trail."""
    s3 = boto3.client("s3", region_name="us-east-1")
    for name in ("ccp-blocked", "ccp-open", "ccp-trail-logs"):
        s3.create_bucket(Bucket=name)
    for name in ("ccp-blocked", "ccp-trail-logs"):
        s3.put_public_access_block(
            Bucket=name,
            PublicAccessBlockConfiguration=dict.fromkeys(
                (
                    "BlockPublicAcls",
                    "IgnorePublicAcls",
                    "BlockPublicPolicy",
                    "RestrictPublicBuckets",
                ),
                True,
            ),
        )
    trail = boto3.client("cloudtrail", region_name="us-east-1")
    trail.create_trail(Name="org", S3BucketName="ccp-trail-logs", IsMultiRegionTrail=False)
    trail.start_logging(Name="org")
    iam = boto3.client("iam", region_name="us-east-1")
    iam.create_user(UserName="alice")
    iam.create_login_profile(UserName="alice", Password="Pa55word!long-enough")


def pull_and_evaluate(
    settings: Settings, acme: ScopedDB, at: datetime, tests: tuple[str, ...]
) -> None:
    """One pull of the moto account as of `at`, normalized and evaluated (inside mock_aws)."""

    def client(credentials: Any, service: str) -> Any:
        make: Any = boto3.client
        return make(service, region_name="us-east-1")

    pulled = AwsConnector(
        ACCOUNT, f"arn:aws:iam::{ACCOUNT}:role/CCPReadOnly", "ccp-x" * 8, client, clock=lambda: at
    )
    run = sync_connector(settings, acme, acme.tenant, pulled, zone=RawZone(settings.raw_dir))
    assert run.status == "complete", run.error
    normalize_all(acme, settings)
    evaluations.enqueue_all(acme, settings, now=at, tests=tests)
    evaluations.drain(acme, settings, now=at)


@contextmanager
def failing_account(
    base_settings: Settings, tmp_path: Path, prefix: str, tests: tuple[str, ...]
) -> Iterator[tuple[Settings, ScopedDB]]:
    """A scratch database holding one pull of the moto account above, evaluated; the moto
    account stays up while the caller runs, so it can change it and pull again."""
    with scratch_database(base_settings, prefix=prefix) as settings:
        local = settings.model_copy(update={"ccp_data_dir": tmp_path / "data"})
        pool = app_pool(local, max_size=2)
        try:
            acme = ScopedDB(pool, TenantContext.for_id(local, "acme"))
            with mock_aws():
                aws_world()
                pull_and_evaluate(local, acme, NOW, tests)
                yield local, acme
        finally:
            pool.close()
