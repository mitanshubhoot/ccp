"""The cloud inventory and AWS's resource-type registry (spec.md A-166).

take        once a day per AWS connection: every resource AWS Config records, landed
            as raw evidence first, then one `resource_inventory` snapshot; a recorder
            that is off gives an incomplete snapshot, so coverage says not assessed
            rather than reading an empty account as a covered one
registry    once a week: AWS's public resource types from the CloudFormation registry,
            compared with the last list kept; a new type joins the queue with a
            proposed class for a person to approve, before a customer adopts it
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ulid import ULID

from ccp.config import Settings
from ccp.connectors.aws.connector import inventory_of
from ccp.evaluators import jobs
from ccp.ingest.landing import land_received
from ccp.ingest.raw_zone import open_zone
from ccp.knowledge import service_classes
from ccp.lookups.runner import Connectors, default_connectors
from ccp.storage.scoped import ScopedDB

REGISTRY_DIR = "registry"


@dataclass
class Taken:
    snapshot_id: str | None
    complete: bool
    resources: int
    detail: str = ""
    unclassified: list[str] = field(default_factory=list)


def take(db: ScopedDB, settings: Settings, connectors: Connectors | None = None) -> Taken:
    """One inventory snapshot of the tenant's AWS account."""
    aws = (connectors or default_connectors(db, settings))("aws")
    if aws is None:
        return Taken(None, False, 0, "no live AWS connection")
    complete, detail = True, ""
    try:
        found = inventory_of(aws) if hasattr(aws, "_assumed") else aws.inventory()
    except Exception as exc:  # recorded as an incomplete snapshot, whatever the cause
        found, detail = None, f"{type(exc).__name__}: {exc}"
    if found is None:
        complete = False
        detail = detail or "AWS Config is not recording: the inventory is not whole"
    ids = land_received(
        db, open_zone(settings), "aws", list(getattr(aws, "responses", ())), "inventory"
    )
    snapshot_id = str(ULID())
    db.execute(
        "INSERT INTO resource_inventory (tenant_id, snapshot_id, account, complete, resources,"
        " detail, response_ids) VALUES (%s, %s, %s, %s, %s, %s, %s)",
        (
            db.tenant.tenant_id,
            snapshot_id,
            str(getattr(aws, "account_id", "")),
            complete,
            json.dumps(found or []),
            detail,
            json.dumps(ids),
        ),
    )
    classes = service_classes.load()
    unknown = sorted(
        {r["resource_type"] for r in found or [] if r["resource_type"] not in classes.types}
    )
    for resource_type in unknown:  # first seen in a customer's account: queued at once
        queue(settings, resource_type, "customer")
    return Taken(snapshot_id, complete, len(found or []), detail, unknown)


def latest(db: ScopedDB) -> list[dict[str, Any]]:
    """The newest snapshot of each account."""
    rows = db.fetch_all(
        "SELECT DISTINCT ON (account) snapshot_id, account, complete, resources, detail, taken_at"
        " FROM resource_inventory ORDER BY account, taken_at DESC, snapshot_id DESC"
    )
    return [
        {
            "snapshot_id": r[0],
            "account": r[1],
            "complete": r[2],
            "resources": json.loads(r[3]) if isinstance(r[3], str) else r[3],
            "detail": r[4],
            "taken_at": r[5],
        }
        for r in rows
    ]


# -- the registry -------------------------------------------------------------------------------

Lister = Callable[[], list[str]]  # AWS's public resource types


def cloudformation_types(settings: Settings) -> list[str]:
    """Every public resource type in the CloudFormation registry, read with CCP's own
    credentials (no customer account is touched)."""
    import boto3

    make: Any = boto3.client
    client = make("cloudformation", region_name=settings.aws_region)
    names: list[str] = []
    token: str | None = None
    while True:
        kwargs: dict[str, Any] = {"Visibility": "PUBLIC", "Type": "RESOURCE", "MaxResults": 100}
        if token:
            kwargs["NextToken"] = token
        page = client.list_types(**kwargs)
        names.extend(str(t["TypeName"]) for t in page.get("TypeSummaries", []))
        token = page.get("NextToken")
        if not token:
            return sorted(set(names))


def _dir(settings: Settings) -> Path:
    path = settings.ccp_data_dir / REGISTRY_DIR
    path.mkdir(parents=True, exist_ok=True)
    return path


def queue(settings: Settings, resource_type: str, seen_in: str) -> bool:
    """Queue a type for a class decision and a drafted test, once."""
    path = _dir(settings) / "queue.jsonl"
    queued = {
        json.loads(line)["resource_type"]
        for line in (path.read_text(encoding="utf-8").splitlines() if path.exists() else [])
        if line.strip()
    }
    if resource_type in queued:
        return False
    entry = {
        "resource_type": resource_type,
        "seen_in": seen_in,
        "first_seen": datetime.now(UTC).isoformat(),
        "proposed_class": service_classes.propose_class(resource_type),
    }
    with path.open("a", encoding="utf-8", newline="\n") as out:
        out.write(json.dumps(entry) + "\n")
    return True


def registry_diff(
    settings: Settings, lister: Lister | None = None, now: datetime | None = None
) -> list[str]:
    """Today's registry against the last list kept; the new types are queued."""
    moment = now or datetime.now(UTC)
    folder = _dir(settings)
    today = folder / f"aws-types-{moment.date().isoformat()}.json"
    if today.exists():
        return []
    previous = sorted(folder.glob("aws-types-*.json"))
    names = (lister or (lambda: cloudformation_types(settings)))()
    today.write_text(json.dumps(names), encoding="utf-8")
    if not previous:
        return []  # the first list is the baseline
    before = set(json.loads(previous[-1].read_text(encoding="utf-8")))
    new = sorted(set(names) - before)
    for resource_type in new:
        queue(settings, resource_type, "registry")
    return new


def _registry_job(db: ScopedDB, settings: Settings, slot: datetime) -> int:
    if slot.weekday() != 0:  # Mondays: the registry is global, so once a week is plenty
        return 0
    return len(registry_diff(settings, now=slot))


jobs.JOBS["inventory"] = lambda db, settings, slot: take(db, settings).resources
jobs.JOBS["registry_diff"] = _registry_job
