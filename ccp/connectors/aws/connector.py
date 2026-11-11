"""The AWS connector (spec.md A-140): configuration read through the onboarded role.

    account     the account's own S3 public access block and its CloudTrail trails
    buckets     every bucket, its own public access block, whether its policy is public,
                and the effective setting: a block holds when the account or the bucket sets
                it, so the S3 test judges what AWS enforces, not either half alone
    iam_users   every IAM user, by its immutable id: console access and MFA

AWS Config first: when a recorder is recording, buckets come from its resource inventory;
otherwise, or when Config does not answer, the same facts come from live reads. Either way
each pull lands a snapshot of what it saw, because AWS keeps only the current setting and
would otherwise overwrite the history an audit needs. A snapshot's event id comes from its
content, so an unchanged setting lands nothing new. Every response is kept as bytes with
AWS's request id (A-136), through botocore's after-call hook.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

from ccp.connectors.aws import onboarding
from ccp.connectors.base import (
    ConnectorName,
    PullError,
    Received,
    SourceEvent,
    SourceKind,
    SyncReport,
)
from ccp.connectors.github.client import content_id
from ccp.tenancy import TenantContext

BLOCKS = ("BlockPublicAcls", "IgnorePublicAcls", "BlockPublicPolicy", "RestrictPublicBuckets")
NO_BLOCK = dict.fromkeys(BLOCKS, False)
Session = Callable[[dict[str, Any] | None, str], Any]  # (credentials, service) -> client


def _code(exc: Exception) -> str:
    response = getattr(exc, "response", None)
    if isinstance(response, dict):
        return str(response.get("Error", {}).get("Code", ""))
    return ""


def effective(account_block: dict[str, bool], bucket_block: dict[str, bool]) -> dict[str, bool]:
    """What S3 enforces: each flag holds when the account or the bucket sets it."""
    return {k: bool(account_block.get(k)) or bool(bucket_block.get(k)) for k in BLOCKS}


class AwsConnector:
    name: ConnectorName = "aws"
    source: SourceKind = "live"
    scopes: tuple[str, ...] = ("account", "buckets", "iam_users")

    def __init__(
        self,
        account_id: str,
        role_arn: str,
        external_id: str,
        client: Session,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.account_id = account_id
        self.role_arn = role_arn
        self.external_id = external_id
        self._client = client
        self._clock = clock or (lambda: datetime.now(UTC))
        self.responses: list[Received] = []

    # -- sessions -----------------------------------------------------------------------

    def _watch(self, client: Any) -> Any:
        def keep(http_response: Any, parsed: Any, model: Any, **_: Any) -> None:
            meta = parsed.get("ResponseMetadata", {}) if isinstance(parsed, dict) else {}
            self.responses.append(
                Received(
                    "POST",
                    f"{client.meta.service_model.service_name}:{model.name}",
                    int(getattr(http_response, "status_code", 0) or 0),
                    meta.get("RequestId"),
                    bytes(getattr(http_response, "content", b"") or b""),
                    self._clock(),
                )
            )

        events = getattr(getattr(client, "meta", None), "events", None)
        if events is not None:
            events.register("after-call.*.*", keep)
        return client

    def _assumed(self) -> dict[str, Any]:
        sts = self._client(None, "sts")
        try:
            answer = sts.assume_role(
                RoleArn=self.role_arn, RoleSessionName="ccp-pull", ExternalId=self.external_id
            )
        except Exception as exc:
            raise PullError(f"cannot assume {self.role_arn}: {_code(exc) or exc}", "auth") from exc
        credentials: dict[str, Any] = answer["Credentials"]
        return credentials

    # -- the daily check (A-121, A-139) -------------------------------------------------

    def check(self, tenant: TenantContext) -> tuple[str, str]:
        result = onboarding.verify(
            self.account_id,
            self.role_arn,
            self.external_id,
            self._client(None, "sts"),
            lambda creds: self._client(creds, "iam"),
        )
        if result.ok:
            return "ok", f"role {self.role_arn} verified"
        return "auth", "; ".join(result.findings)

    # -- the pull -----------------------------------------------------------------------

    def _event(
        self, tenant: TenantContext, kind: str, key: str, payload: dict[str, Any]
    ) -> SourceEvent:
        return SourceEvent(
            tenant_id=tenant.tenant_id,
            connector="aws",
            source_event_id=content_id(f"aws:{kind}:{key}", payload),
            source_ts=self._clock(),
            event_type=f"aws.{kind}.snapshot",
            payload=payload,
            schema_version="aws.v1",
        )

    def _account_block(self, creds: dict[str, Any]) -> dict[str, bool]:
        control = self._watch(self._client(creds, "s3control"))
        try:
            found = control.get_public_access_block(AccountId=self.account_id)
        except Exception as exc:
            if _code(exc) == "NoSuchPublicAccessBlockConfiguration":
                return dict(NO_BLOCK)
            raise
        config = found["PublicAccessBlockConfiguration"]
        return {k: bool(config.get(k)) for k in BLOCKS}

    def _buckets_from_config(self, creds: dict[str, Any]) -> list[dict[str, Any]] | None:
        """Buckets from AWS Config's inventory, or None when no recorder is recording."""
        config = self._watch(self._client(creds, "config"))
        try:
            status = config.describe_configuration_recorder_status()
            if not any(r.get("recording") for r in status.get("ConfigurationRecordersStatus", [])):
                return None
            answer = config.select_resource_config(
                Expression="SELECT resourceName, supplementaryConfiguration"
                " WHERE resourceType = 'AWS::S3::Bucket'"
            )
        except Exception:
            return None  # Config is off or not permitted: read live instead
        out: list[dict[str, Any]] = []
        for raw in answer.get("Results", []):
            item = json.loads(raw)
            extra = item.get("supplementaryConfiguration") or {}
            block = extra.get("PublicAccessBlockConfiguration") or {}
            out.append(
                {
                    "bucket": item["resourceName"],
                    # Config writes the flags in lowerCamel (blockPublicAcls)
                    "bucket_block": {
                        k: bool(block.get(k[0].lower() + k[1:], block.get(k))) for k in BLOCKS
                    },
                    "policy_is_public": None,  # Config does not say; the blocks decide
                    "from": "config",
                }
            )
        return out

    def _buckets_live(self, creds: dict[str, Any]) -> list[dict[str, Any]]:
        s3 = self._watch(self._client(creds, "s3"))
        out: list[dict[str, Any]] = []
        for bucket in s3.list_buckets().get("Buckets", []):
            name = bucket["Name"]
            try:
                config = s3.get_public_access_block(Bucket=name)["PublicAccessBlockConfiguration"]
                block = {k: bool(config.get(k)) for k in BLOCKS}
            except Exception as exc:
                if _code(exc) != "NoSuchPublicAccessBlockConfiguration":
                    raise
                block = dict(NO_BLOCK)
            try:
                public: bool | None = bool(
                    s3.get_bucket_policy_status(Bucket=name)["PolicyStatus"]["IsPublic"]
                )
            except Exception as exc:
                if _code(exc) not in ("NoSuchBucketPolicy", "NoSuchPolicy"):
                    raise
                public = False
            out.append(
                {"bucket": name, "bucket_block": block, "policy_is_public": public, "from": "live"}
            )
        return out

    def _trails(self, creds: dict[str, Any]) -> list[dict[str, Any]]:
        trail = self._watch(self._client(creds, "cloudtrail"))
        out: list[dict[str, Any]] = []
        for found in trail.describe_trails(includeShadowTrails=False).get("trailList", []):
            status = trail.get_trail_status(Name=found["TrailARN"])
            out.append(
                {
                    "name": found["Name"],
                    "multi_region": bool(found.get("IsMultiRegionTrail")),
                    "logging": bool(status.get("IsLogging")),
                }
            )
        return out

    def _users(self, creds: dict[str, Any]) -> list[dict[str, Any]]:
        iam = self._watch(self._client(creds, "iam"))
        out: list[dict[str, Any]] = []
        for page in iam.get_paginator("list_users").paginate():
            for user in page.get("Users", []):
                name = user["UserName"]
                try:
                    iam.get_login_profile(UserName=name)
                    console = True
                except Exception as exc:
                    if _code(exc) != "NoSuchEntity":
                        raise
                    console = False
                devices = iam.list_mfa_devices(UserName=name).get("MFADevices", [])
                out.append(
                    {
                        "user_id": user["UserId"],  # immutable: a renamed user keeps it
                        "user_name": name,
                        "arn": user["Arn"],
                        "console_access": console,
                        "mfa_enabled": bool(devices),
                    }
                )
        return out

    def pull(
        self,
        tenant: TenantContext,
        since: datetime | None = None,
        report: SyncReport | None = None,
    ) -> Iterator[SourceEvent]:
        report = report or SyncReport(self.scopes)
        self.responses = []
        creds = self._assumed()
        account_block = self._account_block(creds)
        trails = self._trails(creds)
        account = {
            "account_id": self.account_id,
            "account_block": account_block,
            "trails": trails,
            "multi_region_trail_logging": any(t["multi_region"] and t["logging"] for t in trails),
        }
        yield self._event(tenant, "account", self.account_id, account)
        report.complete("account", 1)
        buckets = self._buckets_from_config(creds)
        if buckets is None:
            buckets = self._buckets_live(creds)
        for bucket in buckets:
            payload = {
                **bucket,
                "account_id": self.account_id,
                "account_block": account_block,
                "effective_block": effective(account_block, bucket["bucket_block"]),
            }
            yield self._event(tenant, "s3.bucket", f"{self.account_id}:{bucket['bucket']}", payload)
        report.complete("buckets", len(buckets))
        users = self._users(creds)
        for user in users:
            payload = {**user, "account_id": self.account_id}
            yield self._event(tenant, "iam.user", user["user_id"], payload)
        report.complete("iam_users", len(users))

    # -- lookups about one failing resource (A-158) --------------------------------------
    # Code chooses each read from a failing verdict; every response is kept as bytes, like
    # a pull's, and the caller lands them as raw evidence before it stores the result.

    def config_history(
        self, resource_type: str, resource_id: str, start: datetime, end: datetime
    ) -> list[dict[str, Any]]:
        """AWS Config's recorded states of one resource, oldest first, read in 7-day
        windows (Config answers a window at a time and pages within it)."""
        self.responses = []
        config = self._watch(self._client(self._assumed(), "config"))
        items: list[dict[str, Any]] = []
        for earlier, later in windows(start, end):
            token: str | None = None
            while True:
                kwargs: dict[str, Any] = {
                    "resourceType": resource_type,
                    "resourceId": resource_id,
                    "earlierTime": earlier,
                    "laterTime": later,
                    "chronologicalOrder": "Forward",
                    "limit": 100,
                }
                if token:
                    kwargs["nextToken"] = token
                try:
                    page = config.get_resource_config_history(**kwargs)
                except Exception as exc:
                    if _code(exc) in (
                        "ResourceNotDiscoveredException",
                        "NoAvailableConfigurationRecorderException",
                    ):
                        return items
                    raise
                items.extend(page.get("configurationItems", []))
                token = page.get("nextToken")
                if not token:
                    break
        return items

    def trail_events(
        self,
        key: str,
        value: str,
        start: datetime,
        end: datetime,
        bucket: Any | None = None,
        max_pages: int = 10,
    ) -> list[dict[str, Any]]:
        """CloudTrail's management events for one lookup attribute (ResourceName or
        EventSource), newest first. LookupEvents allows 2 requests a second per account and
        region, so every page first spends from a bucket that refills at that rate."""
        self.responses = []
        trail = self._watch(self._client(self._assumed(), "cloudtrail"))
        events: list[dict[str, Any]] = []
        token: str | None = None
        for _ in range(max_pages):
            if bucket is not None:
                bucket.take()
            kwargs: dict[str, Any] = {
                "LookupAttributes": [{"AttributeKey": key, "AttributeValue": value}],
                "StartTime": start,
                "EndTime": end,
                "MaxResults": 50,
            }
            if token:
                kwargs["NextToken"] = token
            page = trail.lookup_events(**kwargs)
            events.extend(page.get("Events", []))
            token = page.get("NextToken")
            if not token:
                break
        return events


WINDOW = timedelta(days=7)


def windows(start: datetime, end: datetime) -> list[tuple[datetime, datetime]]:
    """[start, end) cut into 7-day windows, the last one ending at end."""
    out: list[tuple[datetime, datetime]] = []
    at = start
    while at < end:
        out.append((at, min(at + WINDOW, end)))
        at += WINDOW
    return out


def inventory_of(connector: AwsConnector) -> list[dict[str, str]] | None:
    """Every resource AWS Config records in the account, by type (A-166); None when no
    recorder is recording, so the inventory is not whole and must not be read as such."""
    connector.responses = []
    config = connector._watch(connector._client(connector._assumed(), "config"))
    status = config.describe_configuration_recorder_status()
    if not any(r.get("recording") for r in status.get("ConfigurationRecordersStatus", [])):
        return None
    out: list[dict[str, str]] = []
    token: str | None = None
    while True:
        kwargs: dict[str, Any] = {
            "Expression": "SELECT resourceType, resourceId, resourceName, awsRegion",
            "Limit": 100,
        }
        if token:
            kwargs["NextToken"] = token
        page = config.select_resource_config(**kwargs)
        for raw in page.get("Results", []):
            item = json.loads(raw)
            out.append(
                {
                    "resource_type": str(item.get("resourceType")),
                    "resource_id": str(item.get("resourceId")),
                    "name": str(item.get("resourceName") or item.get("resourceId")),
                    "region": str(item.get("awsRegion") or ""),
                }
            )
        token = page.get("NextToken")
        if not token:
            break
    return out
