"""The immutable raw zone (spec.md Layer 2; A-136).

    {tenant_id}/{connector}/{source_ts:%Y/%m/%d}/{source_event_id_safe}.json
    {tenant_id}/{connector}/responses/{sha256}.bin       the provider's bytes, as received

Each event object is the SourceEvent plus ``received_at`` and a sha256 of the payload,
written once. When the primary key already exists, the new copy goes under a ``dups/``
sibling instead: the raw zone records that the provider sent it twice.

Every landed object has a pointer that says exactly which bytes were stored (A-136): a ref,
the sha256 of the stored bytes (not of CCP's re-serialization of the payload, which is kept
apart as ``payload_sha256`` for de-duplication), and a version id. Responses a connector
received land beside the events, content-addressed, with the provider's request id.

    RawZone       local disk; the version id is the stored bytes' digest
    S3RawZone     a bucket with versioning and Object Lock: each object written in
                  governance mode until its retention date, encrypted with the tenant's own
                  KMS key (SSE-KMS, Bucket Keys on), its SHA-256 checksum checked by S3, and
                  its version id recorded; `hold` puts a legal hold on one version
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import parse_qs, urlparse

from ccp.config import Settings
from ccp.connectors.base import Received, SourceEvent, sha256_json

_SAFE = re.compile(r"[^A-Za-z0-9._@-]+")


@dataclass(frozen=True)
class LandedFile:
    ref: str  # a local path, or s3://bucket/key?versionId=...
    payload_sha256: str  # CCP's canonical digest of the payload: the de-duplication key
    stored_sha256: str  # the digest of the bytes actually stored
    version_id: str
    duplicate_file: bool
    store: str = "local"

    @property
    def path(self) -> Path:
        """The local path of a local object (the raw zone's original pointer)."""
        if self.store != "local":
            raise ValueError(f"{self.ref} is not a local object")
        return Path(self.ref)


class RawStore(Protocol):
    store: str

    def write(self, event: SourceEvent) -> LandedFile: ...

    def put_response(self, tenant_id: str, connector: str, received: Received) -> LandedFile: ...

    def read_bytes(self, ref: str) -> bytes: ...


def safe_id(source_event_id: str) -> str:
    cleaned = _SAFE.sub("_", source_event_id)
    return cleaned[:180]


def event_key(event: SourceEvent) -> str:
    day = event.source_ts.strftime("%Y/%m/%d")
    return f"{event.tenant_id}/{event.connector}/{day}/{safe_id(event.source_event_id)}.json"


def _record(event: SourceEvent, received_at: datetime) -> tuple[bytes, str]:
    digest = sha256_json(event.payload)
    record: dict[str, Any] = {
        **event.model_dump(mode="json"),
        "received_at": received_at.isoformat(),
        "payload_sha256": digest,
    }
    return json.dumps(record, ensure_ascii=False, sort_keys=True).encode("utf-8"), digest


def parse_event(data: bytes) -> SourceEvent:
    record = json.loads(data.decode("utf-8"))
    record.pop("received_at", None)
    record.pop("payload_sha256", None)
    return SourceEvent.model_validate(record)


class RawZone:
    """The raw zone on local disk (development and tests)."""

    store = "local"

    def __init__(self, root: Path) -> None:
        self.root = root

    def primary_path(self, event: SourceEvent) -> Path:
        return self.root / event_key(event)

    def _put(self, target: Path, body: bytes) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(target.name + ".tmp")
        tmp.write_bytes(body)
        os.replace(tmp, target)

    def write(self, event: SourceEvent) -> LandedFile:
        received_at = datetime.now(UTC)
        primary = self.primary_path(event)
        if primary.exists():
            stamp = received_at.strftime("%Y%m%dT%H%M%S%fZ")
            target = primary.parent / "dups" / f"{primary.stem}.{stamp}.json"
            duplicate = True
        else:
            target = primary
            duplicate = False
        body, digest = _record(event, received_at)
        self._put(target, body)
        stored = hashlib.sha256(body).hexdigest()
        return LandedFile(str(target), digest, stored, stored[:32], duplicate)

    def put_response(self, tenant_id: str, connector: str, received: Received) -> LandedFile:
        target = self.root / tenant_id / connector / "responses" / f"{received.sha256}.bin"
        duplicate = target.exists()
        if not duplicate:
            self._put(target, received.body)
        return LandedFile(
            str(target), received.sha256, received.sha256, received.sha256[:32], duplicate
        )

    def read_bytes(self, ref: str) -> bytes:
        return Path(ref).read_bytes()

    def read(self, path: Path | str) -> SourceEvent:
        return parse_event(Path(path).read_bytes())


class S3RawZone:
    """The raw zone in S3 (A-136). The bucket is created with Object Lock (Terraform,
    infra/terraform), so versioning is on and a locked version cannot be deleted or
    overwritten before its date without the governance bypass."""

    store = "s3"

    def __init__(
        self,
        bucket: str,
        client: Any,
        *,
        retain_days: int = 400,
        kms_alias: str = "alias/ccp-raw-{tenant}",
    ) -> None:
        self.bucket = bucket
        self.client = client
        self.retain_days = retain_days
        self.kms_alias = kms_alias

    def _exists(self, key: str) -> bool:
        try:
            self.client.head_object(Bucket=self.bucket, Key=key)
        except self.client.exceptions.ClientError as exc:
            if exc.response.get("Error", {}).get("Code") in ("404", "NoSuchKey", "NotFound"):
                return False
            raise
        return True

    def _put(self, tenant_id: str, key: str, body: bytes) -> str:
        answer = self.client.put_object(
            Bucket=self.bucket,
            Key=key,
            Body=body,
            ChecksumAlgorithm="SHA256",
            ServerSideEncryption="aws:kms",
            SSEKMSKeyId=self.kms_alias.format(tenant=tenant_id),
            BucketKeyEnabled=True,
            ObjectLockMode="GOVERNANCE",
            ObjectLockRetainUntilDate=datetime.now(UTC) + timedelta(days=self.retain_days),
        )
        return str(answer["VersionId"])

    def _ref(self, key: str, version_id: str) -> str:
        return f"s3://{self.bucket}/{key}?versionId={version_id}"

    def write(self, event: SourceEvent) -> LandedFile:
        received_at = datetime.now(UTC)
        key = event_key(event)
        duplicate = self._exists(key)
        if duplicate:
            stamp = received_at.strftime("%Y%m%dT%H%M%S%fZ")
            stem, _, _ = key.rpartition(".json")
            folder, _, name = stem.rpartition("/")
            key = f"{folder}/dups/{name}.{stamp}.json"
        body, digest = _record(event, received_at)
        version = self._put(event.tenant_id, key, body)
        stored = hashlib.sha256(body).hexdigest()
        return LandedFile(self._ref(key, version), digest, stored, version, duplicate, "s3")

    def put_response(self, tenant_id: str, connector: str, received: Received) -> LandedFile:
        key = f"{tenant_id}/{connector}/responses/{received.sha256}.bin"
        duplicate = self._exists(key)
        if duplicate:
            head = self.client.head_object(Bucket=self.bucket, Key=key)
            version = str(head["VersionId"])
        else:
            version = self._put(tenant_id, key, received.body)
        return LandedFile(
            self._ref(key, version), received.sha256, received.sha256, version, duplicate, "s3"
        )

    def read_bytes(self, ref: str) -> bytes:
        bucket, key, version = split_s3(ref)
        answer = self.client.get_object(Bucket=bucket, Key=key, VersionId=version)
        body: bytes = answer["Body"].read()
        return body

    def read(self, ref: str) -> SourceEvent:
        return parse_event(self.read_bytes(ref))

    def hold(self, ref: str) -> None:
        """A legal hold on one version: it outlives its retention date until released."""
        bucket, key, version = split_s3(ref)
        self.client.put_object_legal_hold(
            Bucket=bucket, Key=key, VersionId=version, LegalHold={"Status": "ON"}
        )


def split_s3(ref: str) -> tuple[str, str, str]:
    parsed = urlparse(ref)
    if parsed.scheme != "s3":
        raise ValueError(f"{ref!r} is not an s3 ref")
    version = parse_qs(parsed.query).get("versionId", [""])[0]
    if not version:
        raise ValueError(f"{ref!r} names no version")
    return parsed.netloc, parsed.path.lstrip("/"), version


def open_zone(settings: Settings) -> RawZone | S3RawZone:
    """The raw zone this deployment writes to: local disk, or the S3 bucket."""
    if settings.ccp_raw_store == "s3":
        if not settings.ccp_raw_bucket:
            raise RuntimeError("CCP_RAW_STORE is s3 but CCP_RAW_BUCKET is not set")
        import boto3

        client = boto3.client("s3", region_name=settings.aws_region)
        return S3RawZone(settings.ccp_raw_bucket, client, retain_days=settings.ccp_raw_retain_days)
    return RawZone(settings.raw_dir)


def read_bytes(ref: str, settings: Settings | None = None) -> bytes:
    """The stored bytes behind any ref: a local path, or an S3 version."""
    if ref.startswith("s3://"):  # whichever store this deployment writes to now
        import boto3

        bucket, _, _ = split_s3(ref)
        region = (settings or Settings()).aws_region
        return S3RawZone(bucket, boto3.client("s3", region_name=region)).read_bytes(ref)
    return Path(ref).read_bytes()


def read_ref(ref: str, settings: Settings | None = None) -> SourceEvent:
    return parse_event(read_bytes(ref, settings))
