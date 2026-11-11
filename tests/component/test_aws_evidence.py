"""MT6 (A-139, A-140): AWS accounts onboard to a read-only role CCP assumes with its own
external ID, and the connector reads what the S3, IAM and CloudTrail tests judge.

The defects: CCP had no cloud evidence at all; and the usual ways of reading a customer's
account fail open: a role that trusts any caller who names it (no external ID), a role with
ReadOnlyAccess that reads data rather than configuration, and an S3 test that judges a
bucket's own setting while the account's block is what AWS enforces, or the reverse.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import boto3
import psycopg
import pytest
import yaml
from moto import mock_aws

from ccp.config import Settings
from ccp.connectors.aws import onboarding
from ccp.connectors.aws.connector import AwsConnector, effective
from ccp.evaluators import runner as evaluations
from ccp.evaluators.verdict import latest_per_subject
from ccp.ingest import aws_accounts
from ccp.ingest.landing import sync_connector
from ccp.ingest.raw_zone import RawZone
from ccp.normalize.consumer import normalize_all
from ccp.storage.db import app_pool, scratch_database
from ccp.storage.scoped import ScopedDB
from ccp.tenancy import TenantContext

ACCOUNT = "123456789012"
ROLE = f"arn:aws:iam::{ACCOUNT}:role/CCPReadOnly"
TEMPLATE = Path(__file__).resolve().parents[2] / "infra" / "cloudformation" / "ccp-read-role.yaml"


class _Denied(Exception):
    def __init__(self, code: str = "AccessDenied") -> None:
        super().__init__(code)
        self.response = {"Error": {"Code": code}}


class _Sts:
    def __init__(self, *, needs_id: bool = True, works: bool = True) -> None:
        self.needs_id, self.works = needs_id, works

    def assume_role(self, **kwargs: Any) -> dict[str, Any]:
        if not self.works or (self.needs_id and "ExternalId" not in kwargs):
            raise _Denied()
        return {"Credentials": {"AccessKeyId": "a", "SecretAccessKey": "b", "SessionToken": "c"}}


class _Iam:
    def __init__(self, attached: list[str] | None = None, allowed: set[str] | None = None) -> None:
        self.attached = attached or ["arn:aws:iam::aws:policy/SecurityAudit"]
        self.allowed = allowed or set()

    def list_attached_role_policies(self, **kwargs: Any) -> dict[str, Any]:
        return {"AttachedPolicies": [{"PolicyArn": arn} for arn in self.attached]}

    def simulate_principal_policy(self, **kwargs: Any) -> dict[str, Any]:
        return {
            "EvaluationResults": [
                {
                    "EvalActionName": a,
                    "EvalDecision": "allowed" if a in self.allowed else "implicitDeny",
                }
                for a in kwargs["ActionNames"]
            ]
        }


def _verify(sts: _Sts, iam: _Iam, role: str = ROLE) -> onboarding.Verification:
    return onboarding.verify(ACCOUNT, role, "ccp-" + "x" * 40, sts, lambda creds: iam)


def test_onboarding_refuses_a_role_that_fails_open() -> None:
    assert _verify(_Sts(), _Iam()).ok
    assert "without the external ID" in _verify(_Sts(needs_id=False), _Iam()).findings[0]
    assert "cannot be assumed" in _verify(_Sts(works=False), _Iam()).findings[0]
    readonly = _Iam(attached=["arn:aws:iam::aws:policy/ReadOnlyAccess"])
    assert "ReadOnlyAccess is attached" in _verify(_Sts(), readonly).findings[0]
    writes = _verify(_Sts(), _Iam(allowed={"s3:PutObject", "secretsmanager:GetSecretValue"}))
    assert sorted(writes.findings) == [
        "the role may s3:PutObject: it must not",
        "the role may secretsmanager:GetSecretValue: it must not",
    ]
    assert "not in account" in _verify(_Sts(), _Iam(), "arn:aws:iam::999:role/x").findings[0]
    ids = {onboarding.new_external_id() for _ in range(50)}
    assert len(ids) == 50 and all(len(i) >= 40 for i in ids)
    url = onboarding.quick_create_url(
        "https://t.example/r.yaml", "ccp-abc", "arn:aws:iam::1:role/x"
    )
    assert "param_ExternalId=ccp-abc" in url and "quickcreate" in url
    with pytest.raises(ValueError):
        onboarding.check_account_id("12345")


class _Cfn(yaml.SafeLoader):
    pass


_Cfn.add_multi_constructor("!", lambda loader, tag, node: {tag: loader.construct_scalar(node)})


def test_the_template_trusts_only_with_the_external_id_and_denies_secrets_and_writes() -> None:
    doc = yaml.load(TEMPLATE.read_text(encoding="utf-8"), Loader=_Cfn)  # noqa: S506
    role = doc["Resources"]["CcpReadRole"]["Properties"]
    (trust,) = role["AssumeRolePolicyDocument"]["Statement"]
    assert trust["Condition"]["StringEquals"]["sts:ExternalId"] == {"Ref": "ExternalId"}
    assert role["ManagedPolicyArns"] == ["arn:aws:iam::aws:policy/SecurityAudit"]
    statements = {s["Sid"]: s for s in role["Policies"][0]["PolicyDocument"]["Statement"]}
    secrets = statements["NoSecretsOrData"]
    assert secrets["Effect"] == "Deny"
    for action in (
        "secretsmanager:GetSecretValue",
        "s3:GetObject",
        "kms:Decrypt",
        "ssm:GetParameter",
    ):
        assert action in secrets["Action"]
    writes = statements["NoWrites"]
    assert writes["Effect"] == "Deny" and "NotAction" in writes
    assert not any(a.startswith(("s3:Put", "iam:Create", "ec2:Run")) for a in writes["NotAction"])
    assert doc["Parameters"]["ExternalId"]["MinLength"] >= 32


def test_the_effective_block_is_either_half() -> None:
    off = dict.fromkeys(
        ("BlockPublicAcls", "IgnorePublicAcls", "BlockPublicPolicy", "RestrictPublicBuckets"), False
    )
    on = dict.fromkeys(off, True)
    assert all(effective(on, off).values()) and all(effective(off, on).values())
    assert not any(effective(off, off).values())


def _world() -> None:
    """A moto account: two buckets (one blocked, one open), two console users (one with MFA),
    and a multi-region trail that is logging."""
    s3 = boto3.client("s3", region_name="us-east-1")
    for name in ("ccp-blocked", "ccp-open"):
        s3.create_bucket(Bucket=name)
    s3.put_public_access_block(
        Bucket="ccp-blocked",
        PublicAccessBlockConfiguration=dict.fromkeys(
            ("BlockPublicAcls", "IgnorePublicAcls", "BlockPublicPolicy", "RestrictPublicBuckets"),
            True,
        ),
    )
    iam = boto3.client("iam", region_name="us-east-1")
    for user in ("alice", "bob"):
        iam.create_user(UserName=user)
        iam.create_login_profile(UserName=user, Password="Pa55word!long-enough")
    device = iam.create_virtual_mfa_device(VirtualMFADeviceName="bob")["VirtualMFADevice"]
    iam.enable_mfa_device(
        UserName="bob",
        SerialNumber=device["SerialNumber"],
        AuthenticationCode1="123456",
        AuthenticationCode2="654321",
    )
    trail = boto3.client("cloudtrail", region_name="us-east-1")
    s3.create_bucket(Bucket="ccp-trail-logs")
    trail.create_trail(Name="org", S3BucketName="ccp-trail-logs", IsMultiRegionTrail=True)
    trail.start_logging(Name="org")


def _connector() -> AwsConnector:
    def client(credentials: dict[str, Any] | None, service: str) -> Any:
        make: Any = boto3.client
        return make(service, region_name="us-east-1")

    return AwsConnector(
        ACCOUNT, ROLE, "ccp-" + "x" * 40, client, clock=lambda: datetime(2026, 9, 11, tzinfo=UTC)
    )


@pytest.fixture
def aws() -> Iterator[None]:
    with mock_aws():
        _world()
        yield


def test_a_pull_reads_the_effective_settings_and_keeps_aws_responses(aws: None) -> None:
    connector = _connector()
    events = list(connector.pull(TenantContext.for_id(Settings(), "acme")))
    kinds = [e.event_type for e in events]
    assert kinds.count("aws.account.snapshot") == 1 and kinds.count("aws.iam.user.snapshot") == 2
    buckets = {
        e.payload["bucket"]: e.payload for e in events if e.event_type == "aws.s3.bucket.snapshot"
    }
    assert all(buckets["ccp-blocked"]["effective_block"].values())
    assert not any(buckets["ccp-open"]["effective_block"].values())
    assert buckets["ccp-open"]["from"] == "live"  # no Config recorder: live reads
    account = next(e.payload for e in events if e.event_type == "aws.account.snapshot")
    assert account["multi_region_trail_logging"] is True
    users = {
        e.payload["user_name"]: e.payload for e in events if e.event_type == "aws.iam.user.snapshot"
    }
    assert users["bob"]["mfa_enabled"] and not users["alice"]["mfa_enabled"]
    assert users["alice"]["user_id"] not in ("alice", users["bob"]["user_id"])  # the id
    again = list(_connector().pull(TenantContext.for_id(Settings(), "acme")))
    assert {e.source_event_id for e in again} == {e.source_event_id for e in events}  # unchanged
    assert connector.responses and any(r.request_id for r in connector.responses)
    s3c = boto3.client("s3control", region_name="us-east-1")
    s3c.put_public_access_block(
        AccountId=ACCOUNT,
        PublicAccessBlockConfiguration=dict.fromkeys(
            ("BlockPublicAcls", "IgnorePublicAcls", "BlockPublicPolicy", "RestrictPublicBuckets"),
            True,
        ),
    )
    later = list(_connector().pull(TenantContext.for_id(Settings(), "acme")))
    opened = next(e.payload for e in later if e.payload.get("bucket") == "ccp-open")
    assert all(opened["effective_block"].values())  # the account's block now covers it


@pytest.fixture
def own(base_settings: Settings, tmp_path: Path) -> Iterator[tuple[Settings, ScopedDB]]:
    try:
        with scratch_database(base_settings, prefix="ccp_aws") as settings:
            local = settings.model_copy(update={"ccp_data_dir": tmp_path / "data"})
            pool = app_pool(local, max_size=2)
            try:
                yield local, ScopedDB(pool, TenantContext.for_id(local, "acme"))
            finally:
                pool.close()
    except psycopg.OperationalError as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"Postgres not reachable: {exc}")


@pytest.mark.db
def test_the_aws_tests_judge_the_pulled_account(own: tuple[Settings, ScopedDB], aws: None) -> None:
    settings, acme = own
    run = sync_connector(settings, acme, acme.tenant, _connector(), zone=RawZone(settings.raw_dir))
    assert run.status == "complete", run.error
    normalize_all(acme, settings)
    now = datetime.now(UTC)
    evaluations.enqueue_all(acme, settings, now=now, tests=("AWS-1", "AWS-2", "AWS-3"))
    evaluations.drain(acme, settings, now=now)
    status = {v.test_id: v for v in latest_per_subject(acme) if v.test_id.startswith("AWS-")}
    assert status["AWS-1"].status.value == "fail" and "ccp-open" in status["AWS-1"].reasoning
    assert "ccp-blocked" not in status["AWS-1"].reasoning
    assert status["AWS-2"].status.value == "fail" and "alice" in status["AWS-2"].reasoning
    assert status["AWS-3"].status.value == "pass"


@pytest.mark.db
def test_an_account_onboards_once_and_records_each_verification(
    own: tuple[Settings, ScopedDB],
) -> None:
    _, acme = own
    first = aws_accounts.start(acme, ACCOUNT, "operator")
    assert first.status == "pending" and aws_accounts.start(acme, ACCOUNT, "operator") == first
    refused = onboarding.verify(
        ACCOUNT, ROLE, first.external_id, _Sts(needs_id=False), lambda c: _Iam()
    )
    assert aws_accounts.record(acme, refused, "operator").status == "refused"
    passed = onboarding.verify(ACCOUNT, ROLE, first.external_id, _Sts(), lambda c: _Iam())
    done = aws_accounts.record(acme, passed, "operator")
    assert done.status == "verified" and done.external_id == first.external_id
    assert json.loads(json.dumps(list(refused.findings)))  # findings kept as JSON
