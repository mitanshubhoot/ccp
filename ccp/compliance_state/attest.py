"""The attestation record: everything CCP asserts outside itself, judged under a
versioned publication policy (spec.md A-169).

An assertion is a set of items, each with its provenance (system_observed,
customer_attested, ai, ai_reviewed), whether it is complete (assessed) and when it was
true. `config/publication_policy.yaml` says, per kind of assertion, which provenance may go
out, how old an item may be, and whether an unassessed item may go out marked. `attest`
judges every item, records one `attestations` row with the policy version, the reasons
and the digest of what was asserted, and returns the decision; the caller refuses to send
what the policy blocks. The record is what CCP said outside itself, and on what terms.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from ulid import ULID

from ccp.config import REPO_ROOT
from ccp.storage.scoped import ScopedDB

POLICY_FILE = REPO_ROOT / "config" / "publication_policy.yaml"


@dataclass(frozen=True)
class Item:
    item_id: str
    provenance: str
    complete: bool
    as_of: datetime


@dataclass(frozen=True)
class Decision:
    attestation_id: str
    allowed: bool
    policy_version: str
    reasons: list[str] = field(default_factory=list)


@lru_cache(maxsize=2)
def policy(path: Path = POLICY_FILE) -> tuple[str, dict[str, dict[str, Any]]]:
    raw = path.read_bytes().replace(b"\r\n", b"\n")
    version = hashlib.sha256(raw).hexdigest()[:12]
    kinds = (yaml.safe_load(raw.decode("utf-8")) or {}).get("kinds") or {}
    return version, {str(k): dict(v) for k, v in kinds.items()}


def judge(
    kind: str, items: list[Item], now: datetime, path: Path = POLICY_FILE
) -> tuple[str, list[str]]:
    """The policy version and every reason an item may not go out."""
    version, kinds = policy(path)
    rules = kinds.get(kind)
    if rules is None:
        return version, [f"no publication rule for {kind!r}"]
    allowed = set(rules.get("provenance") or [])
    max_age = int(rules.get("max_age_days", 365))
    reasons: list[str] = []
    for item in items:
        if item.provenance not in allowed:
            reasons.append(f"{item.item_id}: {item.provenance} may not go out as {kind}")
        if (now - item.as_of).days > max_age:
            reasons.append(f"{item.item_id}: {(now - item.as_of).days} days old; at most {max_age}")
        if not item.complete and not rules.get("allow_incomplete", False):
            reasons.append(f"{item.item_id}: not assessed")
    return version, reasons


def attest(
    db: ScopedDB,
    kind: str,
    ref: str,
    items: list[Item],
    content: Any,
    by: str,
    now: datetime | None = None,
) -> Decision:
    """Judge an assertion and record it, allowed or not."""
    moment = now or datetime.now(UTC)
    version, reasons = judge(kind, items, moment)
    digest = hashlib.sha256(json.dumps(content, sort_keys=True, default=str).encode()).hexdigest()
    attestation_id = str(ULID())
    db.execute(
        "INSERT INTO attestations (tenant_id, attestation_id, kind, ref, policy_version, allowed,"
        " reasons, items, content_sha256, asserted_by) VALUES (%s, %s, %s, %s, %s, %s, %s, %s,"
        " %s, %s)",
        (
            db.tenant.tenant_id,
            attestation_id,
            kind,
            ref,
            version,
            not reasons,
            json.dumps(reasons),
            len(items),
            digest,
            by,
        ),
    )
    return Decision(attestation_id, not reasons, version, reasons)


def provenance_of_result(result: dict[str, Any]) -> str:
    """A package result's provenance: a rule over providers' data, over uploads, or a
    model's reading."""
    if int(result.get("method", {}).get("tier") or 1) > 1:
        return "ai"
    uploads = [
        e for e in result.get("evidence", []) if str(e.get("entity_id", "")).startswith("manual:")
    ]
    return "customer_attested" if uploads else "system_observed"
