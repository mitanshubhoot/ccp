"""MT5 (A-136): every raw pointer names the exact bytes stored and their version, and the
provider's responses are kept as sent, with its request id.

The defects: the stored hash covered CCP's own re-serialization of the payload, not the bytes
stored or the bytes received; the raw zone lived on local disk with no versions, so nothing
could say which object a row pointed at once it changed; and nothing recorded what the
provider actually sent.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path
from typing import Any

import boto3
import httpx
import psycopg
import pytest
from moto import mock_aws

from ccp.config import Settings
from ccp.connectors.base import SourceEvent
from ccp.connectors.github.client import GitHubLiveConnector
from ccp.ingest import raw_audit
from ccp.ingest.landing import land_events, land_responses
from ccp.ingest.raw_zone import RawZone, S3RawZone, read_ref
from ccp.normalize.consumer import normalize_all
from ccp.storage.db import app_pool, scratch_database
from ccp.storage.scoped import ScopedDB
from ccp.tenancy import TenantContext, TenantId


def _event(event_id: str, ts: str = "2026-08-20T17:00:00Z") -> SourceEvent:
    return SourceEvent(
        tenant_id=TenantId("acme"),
        connector="hris",
        source_event_id=event_id,
        source_ts=datetime.fromisoformat(ts.replace("Z", "+00:00")),
        event_type="hris.employee.snapshot",
        payload={
            "employee_id": event_id,
            "display_name": "Dana Q",
            "work_email": "dana@acme.example",
            "status": "active",
            "hired_at": "2024-01-02T00:00:00Z",
        },
        schema_version="1",
    )


def test_a_local_pointer_names_the_bytes_it_stored(tmp_path: Path) -> None:
    landed = RawZone(tmp_path).write(_event("E1"))
    stored = Path(landed.ref).read_bytes()
    assert landed.stored_sha256 == hashlib.sha256(stored).hexdigest()
    assert landed.version_id == landed.stored_sha256[:32] and landed.store == "local"
    assert landed.payload_sha256 != landed.stored_sha256  # the dedup key is a different digest


@pytest.fixture
def s3() -> Iterator[Any]:
    with mock_aws():
        client = boto3.client("s3", region_name="us-east-1")
        kms = boto3.client("kms", region_name="us-east-1")
        key = kms.create_key()["KeyMetadata"]["KeyId"]
        kms.create_alias(AliasName="alias/ccp-raw-acme", TargetKeyId=key)
        client.create_bucket(Bucket="ccp-raw", ObjectLockEnabledForBucket=True)
        yield client


def test_the_s3_zone_versions_locks_and_encrypts_each_object(s3: Any) -> None:
    zone = S3RawZone("ccp-raw", s3, retain_days=30)
    first, again = zone.write(_event("E1")), zone.write(_event("E1"))
    assert first.ref.startswith("s3://ccp-raw/acme/hris/2026/08/20/E1.json?versionId=")
    assert again.duplicate_file and "/dups/" in again.ref and not first.duplicate_file
    assert read_ref(first.ref) == _event("E1")
    bucket, key = "ccp-raw", "acme/hris/2026/08/20/E1.json"
    head = s3.head_object(Bucket=bucket, Key=key, VersionId=first.version_id)
    assert head["ObjectLockMode"] == "GOVERNANCE" and head["ServerSideEncryption"] == "aws:kms"
    body = s3.get_object(Bucket=bucket, Key=key, VersionId=first.version_id)["Body"].read()
    assert hashlib.sha256(body).hexdigest() == first.stored_sha256
    with pytest.raises(s3.exceptions.ClientError, match="AccessDenied"):  # locked: no delete
        s3.delete_object(Bucket=bucket, Key=key, VersionId=first.version_id)
    zone.hold(first.ref)
    held = s3.get_object_legal_hold(Bucket=bucket, Key=key, VersionId=first.version_id)
    assert held["LegalHold"]["Status"] == "ON"


ORG = "acme-example"


def _github(request: httpx.Request) -> httpx.Response:
    headers = {"x-github-request-id": f"REQ:{request.url.path}"}
    if request.url.path == f"/orgs/{ORG}":
        body: Any = {"node_id": "O_1", "login": ORG, "updated_at": "2026-09-01T00:00:00Z"}
        return httpx.Response(200, json=body, headers=headers)
    if request.url.path == "/graphql":
        return httpx.Response(200, json={"data": {"user": {}}}, headers=headers)
    if request.url.path.startswith(f"/orgs/{ORG}/"):
        return httpx.Response(200, json=[], headers=headers)
    return httpx.Response(404, json={"message": "Not Found"}, headers=headers)


def test_a_live_pull_keeps_each_response_with_its_request_id() -> None:
    connector = GitHubLiveConnector(
        "t", transport=httpx.MockTransport(_github), sleep=lambda s: None
    )
    list(connector.pull(TenantContext.for_id(Settings(), "acme")))
    assert connector.responses
    org = next(r for r in connector.responses if r.path == f"/orgs/{ORG}")
    assert org.request_id == f"REQ:/orgs/{ORG}" and org.status == 200
    assert org.sha256 == hashlib.sha256(org.body).hexdigest() and b'"O_1"' in org.body


@pytest.fixture
def own(base_settings: Settings) -> Iterator[tuple[Settings, ScopedDB]]:
    try:
        with scratch_database(base_settings, prefix="ccp_raw") as settings:
            pool = app_pool(settings, max_size=2)
            try:
                yield settings, ScopedDB(pool, TenantContext.for_id(settings, "acme"))
            finally:
                pool.close()
    except psycopg.OperationalError as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"Postgres not reachable: {exc}")


@pytest.mark.db
def test_landing_in_s3_records_versions_and_normalizes_from_them(
    own: tuple[Settings, ScopedDB], s3: Any
) -> None:
    settings, acme = own
    zone = S3RawZone("ccp-raw", s3)
    assert land_events(acme, zone, [_event("E7")]) == (1, 0)
    row = acme.fetch_one(
        "SELECT raw_path, store, version_id, stored_sha256 FROM raw_events"
        " WHERE source_event_id = 'E7'"
    )
    assert row is not None and row[1] == "s3" and row[2] and str(row[0]).startswith("s3://")
    normalize_all(acme, settings)  # reads the event back from its S3 version
    assert acme.fetch_one("SELECT count(*) FROM persons WHERE entity_id LIKE '%E7%'") != (0,)
    connector = GitHubLiveConnector(
        "t", transport=httpx.MockTransport(_github), sleep=lambda s: None
    )
    list(connector.pull(acme.tenant))
    kept = land_responses(acme, zone, connector, "run-1")
    assert kept == len(connector.responses)
    found = raw_audit.verify(acme, settings)
    assert found.clean and found.checked == 1 + kept and found.unversioned == 0


@pytest.mark.db
def test_the_raw_audit_finds_an_altered_or_missing_object(
    own: tuple[Settings, ScopedDB], tmp_path: Path
) -> None:
    settings, acme = own
    zone = RawZone(tmp_path)
    land_events(acme, zone, [_event("E8"), _event("E9")])
    assert raw_audit.verify(acme, settings).clean
    altered = Path(zone.primary_path(_event("E8")))
    altered.write_bytes(altered.read_bytes().replace(b"Dana Q", b"Dana R"))
    zone.primary_path(_event("E9")).unlink()
    found = raw_audit.verify(acme, settings)
    assert [a.split(":", 1)[0] for a in found.altered] == ["raw event hris"]
    assert len(found.missing) == 1 and "E9" in found.missing[0]
    assert not found.clean
