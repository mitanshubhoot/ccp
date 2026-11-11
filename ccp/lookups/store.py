"""The lookups table (spec.md A-158): what code read about each failing resource.

Insert-only: one row per failing resource and kind, with its typed result and the raw
responses it landed first. Reading it needs no credential, so the Investigator's tool and
the fix suggestions read it here, never through the connector that wrote it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from ulid import ULID

from ccp.knowledge.lookups import LookupKind
from ccp.storage.scoped import ScopedDB

Status = Literal["ok", "empty", "error", "skipped"]

_COLUMNS = (
    "lookup_id, verdict_id, test_id, resource, entity_id, kind, params, status, result,"
    " response_ids, error, created_at"
)


@dataclass(frozen=True)
class Lookup:
    lookup_id: str
    verdict_id: str
    test_id: str
    resource: str
    entity_id: str | None
    kind: LookupKind
    params: dict[str, Any]
    status: Status
    result: dict[str, Any]
    response_ids: list[str]
    error: str | None
    created_at: datetime


def _load(value: Any) -> Any:
    return json.loads(value) if isinstance(value, str) else value


def _row(r: Any) -> Lookup:
    return Lookup(
        r[0],
        r[1],
        r[2],
        r[3],
        r[4],
        r[5],
        _load(r[6]),
        r[7],
        _load(r[8]),
        _load(r[9]),
        r[10],
        r[11],
    )


def record(
    db: ScopedDB,
    *,
    verdict_id: str,
    test_id: str,
    resource: str,
    entity_id: str | None,
    kind: LookupKind,
    params: dict[str, Any],
    status: Status,
    result: dict[str, Any],
    response_ids: list[str],
    error: str | None = None,
) -> str:
    lookup_id = str(ULID())
    db.execute(
        "INSERT INTO lookups (tenant_id, lookup_id, verdict_id, test_id, resource, entity_id,"
        " kind, params, status, result, response_ids, error)"
        " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
        (
            db.tenant.tenant_id,
            lookup_id,
            verdict_id,
            test_id,
            resource,
            entity_id,
            kind,
            json.dumps(params, default=str),
            status,
            json.dumps(result, default=str),
            json.dumps(response_ids),
            error,
        ),
    )
    return lookup_id


def for_verdict(db: ScopedDB, verdict_id: str) -> list[Lookup]:
    rows = db.fetch_all(
        f"SELECT {_COLUMNS} FROM lookups WHERE verdict_id = %s ORDER BY created_at, lookup_id",
        (verdict_id,),
    )
    return [_row(r) for r in rows]


def get(db: ScopedDB, lookup_id: str) -> Lookup | None:
    r = db.fetch_one(f"SELECT {_COLUMNS} FROM lookups WHERE lookup_id = %s", (lookup_id,))
    return _row(r) if r is not None else None


def summary(lookup: Lookup) -> dict[str, Any]:
    """A lookup as the Investigator sees it: the typed result without file text, which the
    fix suggestions render from and a model has no need to read."""
    result = dict(lookup.result)
    if lookup.kind == "infra_code":
        result["files"] = [
            {k: v for k, v in f.items() if k != "text"} for f in result.get("files", [])
        ]
    return {
        "lookup_id": lookup.lookup_id,
        "resource": lookup.resource,
        "kind": lookup.kind,
        "status": lookup.status,
        "result": result,
        "error": lookup.error,
    }
