"""Re-reading the raw zone against its pointers (spec.md A-136).

Every raw_events row names the bytes it stored (stored_sha256) and their version; every
raw_responses row the provider's bytes (body_sha256). `verify` reads each object back by its
ref and hashes it: an object that is gone, or whose bytes no longer hash to what was
recorded, is a finding. A row from before A-136 names no version and is counted apart, so the
share of rows that carry one is reported rather than assumed.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

from ccp.config import Settings
from ccp.ingest import raw_zone
from ccp.storage.scoped import ScopedDB


@dataclass
class RawAudit:
    checked: int = 0
    unversioned: int = 0
    missing: list[str] = field(default_factory=list)
    altered: list[str] = field(default_factory=list)

    @property
    def clean(self) -> bool:
        return not self.missing and not self.altered


def _check(audit: RawAudit, label: str, ref: str, expected: str, settings: Settings) -> None:
    try:
        body = raw_zone.read_bytes(ref, settings)
    except Exception as exc:  # a file gone, or an S3 version gone or unreadable
        audit.missing.append(f"{label}: {ref} ({type(exc).__name__})")
        return
    audit.checked += 1
    if hashlib.sha256(body).hexdigest() != expected:
        audit.altered.append(f"{label}: {ref}")


def verify(db: ScopedDB, settings: Settings) -> RawAudit:
    audit = RawAudit()
    for connector, event_id, ref, stored, version in db.fetch_all(
        "SELECT connector, source_event_id, raw_path, stored_sha256, version_id FROM raw_events"
        " ORDER BY received_at, source_event_id"
    ):
        if stored is None or version is None:
            audit.unversioned += 1
            continue
        _check(audit, f"raw event {connector}:{event_id}", str(ref), str(stored), settings)
    for response_id, ref, digest in db.fetch_all(
        "SELECT response_id, ref, body_sha256 FROM raw_responses ORDER BY received_at"
    ):
        _check(audit, f"response {response_id}", str(ref), str(digest), settings)
    return audit
