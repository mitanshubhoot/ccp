"""Entity persistence and current-state reads, through ScopedDB only.

Writes are append-only observation rows keyed on (entity_id, source_event_id), so
re-processing an event is a no-op. Reads merge observations per entity in observed_at
order: persons merge per field (a partial HR event never blanks a full snapshot),
asset settings merge per key (a repo snapshot and its branch-protection snapshot are
separate events), grants and evidence take the newest row.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

import psycopg

from ccp.normalize.entities import (
    AccessGrant,
    Asset,
    Entity,
    Evidence,
    Finding,
    Person,
    Vendor,
)
from ccp.normalize.merge import merge_assets, merge_persons
from ccp.storage import sequences
from ccp.storage.scoped import ScopedDB
from ccp.tenancy import TenantId
from ccp.types import AccountKind, EmploymentStatus, EvidenceType, GrantState

_TABLE: dict[type[Any], str] = {
    Person: "persons",
    AccessGrant: "access_grants",
    Asset: "assets",
    Evidence: "evidence",
    Vendor: "vendors",
    Finding: "findings",
}


def table_for(entity: Entity) -> str:
    return _TABLE[type(entity)]


def _j(value: Any) -> str:
    return json.dumps(value, default=str)


def insert(cur: psycopg.Cursor[Any], entity: Entity) -> bool:
    """Insert one observation row, stamped with the tenant's observation counter so a read
    can be pinned to a watermark (A-125). Returns False when the row already existed."""
    seq = sequences.next_value(cur, entity.tenant_id, sequences.OBSERVATIONS)
    base = (
        entity.tenant_id,
        entity.entity_id,
        entity.observed_at,
        entity.source_connector,
        entity.source_event_id,
        seq,
    )
    if isinstance(entity, Person):
        row = cur.execute(
            "INSERT INTO persons (tenant_id, entity_id, observed_at, source_connector,"
            " source_event_id, recorded_seq, display_name, emails, handles, employee_id,"
            " account_kind, employment_status, status_effective_at, owner_employee_id)"
            " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)"
            " ON CONFLICT DO NOTHING RETURNING 1",
            (
                *base,
                entity.display_name,
                _j(list(entity.emails)),
                _j(entity.handles),
                entity.employee_id,
                entity.account_kind.value,
                entity.employment_status.value,
                entity.status_effective_at,
                entity.owner_employee_id,
            ),
        ).fetchone()
    elif isinstance(entity, AccessGrant):
        row = cur.execute(
            "INSERT INTO access_grants (tenant_id, entity_id, observed_at, source_connector,"
            " source_event_id, recorded_seq, person_entity_id, asset_entity_id, role, state,"
            " state_effective_at, attributes) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)"
            " ON CONFLICT DO NOTHING RETURNING 1",
            (
                *base,
                entity.person_entity_id,
                entity.asset_entity_id,
                entity.role,
                entity.state.value,
                entity.state_effective_at,
                _j(entity.attributes),
            ),
        ).fetchone()
    elif isinstance(entity, Asset):
        row = cur.execute(
            "INSERT INTO assets (tenant_id, entity_id, observed_at, source_connector,"
            " source_event_id, recorded_seq, name, asset_type, tags, settings)"
            " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING RETURNING 1",
            (*base, entity.name, entity.asset_type, _j(list(entity.tags)), _j(entity.settings)),
        ).fetchone()
    elif isinstance(entity, Evidence):
        row = cur.execute(
            "INSERT INTO evidence (tenant_id, entity_id, observed_at, source_connector,"
            " source_event_id, recorded_seq, evidence_type, title, text, subject_entity_id,"
            " issued_at, raw_ref, page_texts, guardrail_events, page_methods)"
            " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)"
            " ON CONFLICT DO NOTHING RETURNING 1",
            (
                *base,
                entity.evidence_type.value,
                entity.title,
                entity.text,
                entity.subject_entity_id,
                entity.issued_at,
                entity.raw_ref,
                _j(list(entity.page_texts)) if entity.page_texts is not None else None,
                _j(list(entity.guardrail_events)),
                _j(list(entity.page_methods)) if entity.page_methods is not None else None,
            ),
        ).fetchone()
    elif isinstance(entity, Vendor):
        row = cur.execute(
            "INSERT INTO vendors (tenant_id, entity_id, observed_at, source_connector,"
            " source_event_id, recorded_seq, name, vendor_type, data_categories)"
            " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING RETURNING 1",
            (*base, entity.name, entity.vendor_type, _j(list(entity.data_categories))),
        ).fetchone()
    elif isinstance(entity, Finding):
        row = cur.execute(
            "INSERT INTO findings (tenant_id, entity_id, observed_at, source_connector,"
            " source_event_id, recorded_seq, test_id, severity, summary, subject_entity_ids)"
            " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING RETURNING 1",
            (
                *base,
                entity.test_id,
                entity.severity,
                entity.summary,
                _j(list(entity.subject_entity_ids)),
            ),
        ).fetchone()
    else:  # pragma: no cover - the union is closed
        raise TypeError(f"unknown entity {type(entity).__name__}")
    return row is not None


# -- reads ----------------------------------------------------------------------------


def _load(value: Any) -> Any:
    return json.loads(value) if isinstance(value, str) else value


def _visible(as_of: int | None) -> tuple[str, tuple[Any, ...]]:
    """The rows visible at a watermark (A-125): stamped at or below it, or before stamping."""
    if as_of is None:
        return "", ()
    return " WHERE (recorded_seq IS NULL OR recorded_seq <= %s)", (as_of,)


def person_observations(db: ScopedDB, as_of: int | None = None) -> list[Person]:
    where, params = _visible(as_of)
    rows = db.fetch_all(
        "SELECT tenant_id, entity_id, observed_at, source_connector, source_event_id, display_name,"
        " emails, handles, employee_id, account_kind, employment_status, status_effective_at,"
        f" owner_employee_id FROM persons{where} ORDER BY entity_id, observed_at, source_event_id",
        params,
    )
    return [
        Person(
            tenant_id=TenantId(r[0]),
            entity_id=r[1],
            observed_at=r[2],
            source_connector=r[3],
            source_event_id=r[4],
            display_name=r[5],
            emails=tuple(_load(r[6])),
            handles=dict(_load(r[7])),
            employee_id=r[8],
            account_kind=AccountKind(r[9]),
            employment_status=EmploymentStatus(r[10]),
            status_effective_at=r[11],
            owner_employee_id=r[12],
        )
        for r in rows
    ]


def current_persons(db: ScopedDB, as_of: int | None = None) -> list[Person]:
    """Merged view of every person; see ccp.normalize.merge for the rules."""
    return merge_persons(person_observations(db, as_of))


def current_grants(db: ScopedDB, as_of: int | None = None) -> list[AccessGrant]:
    """The newest observation of every grant (as of a watermark when given, A-125)."""
    where, params = _visible(as_of)
    rows = db.fetch_all(
        "SELECT DISTINCT ON (entity_id) tenant_id, entity_id, observed_at, source_connector,"
        " source_event_id, person_entity_id, asset_entity_id, role, state, state_effective_at,"
        f" attributes FROM access_grants{where}"
        " ORDER BY entity_id, observed_at DESC, source_event_id DESC",
        params,
    )
    return [
        AccessGrant(
            tenant_id=TenantId(r[0]),
            entity_id=r[1],
            observed_at=r[2],
            source_connector=r[3],
            source_event_id=r[4],
            person_entity_id=r[5],
            asset_entity_id=r[6],
            role=r[7],
            state=GrantState(r[8]),
            state_effective_at=r[9],
            attributes=dict(_load(r[10])),
        )
        for r in rows
    ]


def current_assets(db: ScopedDB, as_of: int | None = None) -> list[Asset]:
    """Latest scalar fields per asset; settings merged across all observations in time order."""
    where, params = _visible(as_of)
    rows = db.fetch_all(
        "SELECT tenant_id, entity_id, observed_at, source_connector, source_event_id, name,"
        f" asset_type, tags, settings FROM assets{where}"
        " ORDER BY entity_id, observed_at, source_event_id",
        params,
    )
    observations = [
        Asset(
            tenant_id=TenantId(r[0]),
            entity_id=r[1],
            observed_at=r[2],
            source_connector=r[3],
            source_event_id=r[4],
            name=r[5],
            asset_type=r[6],
            tags=tuple(_load(r[7])),
            settings=dict(_load(r[8])),
        )
        for r in rows
    ]
    return merge_assets(observations)


def current_evidence(
    db: ScopedDB, evidence_type: EvidenceType | None = None, as_of: int | None = None
) -> list[Evidence]:
    """The newest observation of every evidence entity (as of a watermark when given)."""
    where, params = _visible(as_of)
    query = (
        "SELECT * FROM (SELECT DISTINCT ON (entity_id) tenant_id, entity_id, observed_at,"
        " source_connector, source_event_id, evidence_type, title, text, subject_entity_id,"
        f" issued_at, raw_ref, page_texts, guardrail_events, page_methods FROM evidence{where}"
        " ORDER BY entity_id, observed_at DESC, source_event_id DESC) latest"
    )
    if evidence_type is not None:
        query += " WHERE evidence_type = %s"
        params = (*params, evidence_type.value)
    rows = db.fetch_all(query + " ORDER BY observed_at DESC, entity_id", params)
    return [_evidence(r) for r in rows]


_EVIDENCE_COLUMNS = (
    "tenant_id, entity_id, observed_at, source_connector, source_event_id, evidence_type, title,"
    " text, subject_entity_id, issued_at, raw_ref, page_texts, guardrail_events, page_methods"
)


def _evidence(r: tuple[Any, ...]) -> Evidence:
    return Evidence(
        tenant_id=TenantId(r[0]),
        entity_id=r[1],
        observed_at=r[2],
        source_connector=r[3],
        source_event_id=r[4],
        evidence_type=EvidenceType(r[5]),
        title=r[6],
        text=r[7],
        subject_entity_id=r[8],
        issued_at=r[9],
        raw_ref=r[10],
        page_texts=tuple(_load(r[11])) if r[11] is not None else None,
        guardrail_events=tuple(_load(r[12])),
        page_methods=tuple(_load(r[13])) if len(r) > 13 and r[13] is not None else None,
    )


def evidence_version(
    db: ScopedDB, entity_id: str, source_event_id: str | None = None
) -> Evidence | None:
    """One observation of an evidence entity: the named one (its version id), else the newest."""
    query = f"SELECT {_EVIDENCE_COLUMNS} FROM evidence WHERE entity_id = %s"  # noqa: S608
    params: tuple[Any, ...] = (entity_id,)
    if source_event_id is not None:
        query += " AND source_event_id = %s"
        params = (*params, source_event_id)
    row = db.fetch_one(query + " ORDER BY observed_at DESC, source_event_id DESC LIMIT 1", params)
    return _evidence(row) if row is not None else None


def current_vendors(db: ScopedDB) -> list[Vendor]:
    rows = db.fetch_all(
        "SELECT tenant_id, entity_id, observed_at, source_connector, source_event_id, name,"
        " vendor_type, data_categories FROM current_vendors ORDER BY entity_id"
    )
    return [
        Vendor(
            tenant_id=TenantId(r[0]),
            entity_id=r[1],
            observed_at=r[2],
            source_connector=r[3],
            source_event_id=r[4],
            name=r[5],
            vendor_type=r[6],
            data_categories=tuple(_load(r[7])),
        )
        for r in rows
    ]


def latest_observed_at(db: ScopedDB) -> datetime | None:
    row = db.fetch_one(
        "SELECT max(o) FROM (SELECT max(observed_at) AS o FROM persons UNION ALL"
        " SELECT max(observed_at) FROM access_grants UNION ALL SELECT max(observed_at) FROM assets"
        " UNION ALL SELECT max(observed_at) FROM evidence) t"
    )
    return row[0] if row else None
