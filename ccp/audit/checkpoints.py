"""Signed checkpoints of each tenant's chain (spec.md A-137).

Hourly, the head of the tenant's chain (its latest seq and entry) is put in a statement,
signed, exported outside the database, and recorded. Someone who can rewrite the database,
rows and chain together, cannot rewrite a signature made with a key they do not hold, nor
an export locked in another account; `verify` finds the chain no longer matching it.

    LocalCheckpointStore   a directory (offline and tests)
    S3CheckpointStore      a bucket in a separate account, each object locked in
                           compliance mode, which nobody, its owner included, can shorten
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

from ccp.audit.signing import Signer, b64, canonical, signer_for
from ccp.config import Settings
from ccp.storage.scoped import ScopedDB


@dataclass(frozen=True)
class Checkpoint:
    tenant_id: str
    seq: int
    entry_sha256: str
    statement: dict[str, Any]
    signature: str
    key_id: str
    exported_to: str
    signed_at: datetime


class CheckpointStore(Protocol):
    def put(self, name: str, body: bytes) -> str: ...

    def get(self, ref: str) -> bytes: ...


class LocalCheckpointStore:
    def __init__(self, root: Path) -> None:
        self.root = root

    def put(self, name: str, body: bytes) -> str:
        target = self.root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            raise FileExistsError(f"{target} exists: a checkpoint is written once")
        target.write_bytes(body)
        return str(target)

    def get(self, ref: str) -> bytes:
        return Path(ref).read_bytes()


class S3CheckpointStore:
    def __init__(self, bucket: str, client: Any, retain_days: int = 2557) -> None:
        self.bucket = bucket
        self.client = client
        self.retain_days = retain_days

    def put(self, name: str, body: bytes) -> str:
        answer = self.client.put_object(
            Bucket=self.bucket,
            Key=name,
            Body=body,
            ChecksumAlgorithm="SHA256",
            ObjectLockMode="COMPLIANCE",
            ObjectLockRetainUntilDate=datetime.now(UTC) + timedelta(days=self.retain_days),
        )
        return f"s3://{self.bucket}/{name}?versionId={answer['VersionId']}"

    def get(self, ref: str) -> bytes:
        from ccp.ingest.raw_zone import split_s3

        bucket, key, version = split_s3(ref)
        body: bytes = self.client.get_object(Bucket=bucket, Key=key, VersionId=version)[
            "Body"
        ].read()
        return body


def store_for(settings: Settings) -> LocalCheckpointStore | S3CheckpointStore:
    if settings.ccp_audit_bucket:
        import boto3

        session = boto3.Session(profile_name=settings.ccp_audit_aws_profile)
        return S3CheckpointStore(
            settings.ccp_audit_bucket, session.client("s3", region_name=settings.aws_region)
        )
    return LocalCheckpointStore(settings.ccp_data_dir / "audit" / "checkpoints")


def head(db: ScopedDB) -> tuple[int, str] | None:
    row = db.fetch_one("SELECT seq, entry_sha256 FROM audit_chain ORDER BY seq DESC LIMIT 1")
    return (int(row[0]), str(row[1])) if row else None


def latest(db: ScopedDB) -> Checkpoint | None:
    rows = recorded(db)
    return rows[-1] if rows else None


def recorded(db: ScopedDB) -> list[Checkpoint]:
    rows = db.fetch_all(
        "SELECT tenant_id, seq, entry_sha256, statement, signature, key_id, exported_to,"
        " signed_at FROM audit_checkpoints ORDER BY seq, signed_at"
    )
    return [
        Checkpoint(
            tenant_id=str(r[0]),
            seq=int(r[1]),
            entry_sha256=str(r[2]),
            statement=dict(json.loads(r[3]) if isinstance(r[3], str) else r[3]),
            signature=str(r[4]),
            key_id=str(r[5]),
            exported_to=str(r[6]),
            signed_at=r[7],
        )
        for r in rows
    ]


def checkpoint(
    db: ScopedDB,
    settings: Settings,
    *,
    signer: Signer | None = None,
    store: CheckpointStore | None = None,
    now: datetime | None = None,
) -> Checkpoint | None:
    """Sign and export the tenant's head; None when the chain is empty or has not moved
    since the last checkpoint."""
    found = head(db)
    if found is None:
        return None
    seq, entry = found
    last = latest(db)
    if last is not None and last.seq == seq:
        return None
    active = signer or signer_for(settings)
    moment = now or datetime.now(UTC)
    statement = {
        "tenant_id": db.tenant.tenant_id,
        "seq": seq,
        "entry_sha256": entry,
        "signed_at": moment.isoformat(),
        "key_id": active.key_id,
    }
    signature = b64(active.sign(canonical(statement)))
    body = json.dumps({"statement": statement, "signature": signature}, sort_keys=True)
    name = f"{db.tenant.tenant_id}/{seq:012d}-{moment:%Y%m%dT%H%M%SZ}.json"
    exported = (store or store_for(settings)).put(name, body.encode("utf-8"))
    db.execute(
        "INSERT INTO audit_checkpoints (tenant_id, seq, entry_sha256, statement, signature,"
        " key_id, exported_to, signed_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
        (
            db.tenant.tenant_id,
            seq,
            entry,
            json.dumps(statement),
            signature,
            active.key_id,
            exported,
            moment,
        ),
    )
    return Checkpoint(
        db.tenant.tenant_id, seq, entry, statement, signature, active.key_id, exported, moment
    )
