"""HRIS SourceEvent -> canonical entities. Pure.

    hris.employee.snapshot -> Person(human), Evidence(hr_roster), Vendor(HRIS)
    hris.hire              -> Person(status active from effective_at)
    hris.role_change       -> Person(unchanged status; observation only)
    hris.termination       -> Person(status terminated from effective_at)
    hris.identity.snapshot -> Asset(identity provider), AccessGrant(person -> idp, with mfa),
                              Evidence(identity_mfa_status)
    hris.account.snapshot  -> Person(kind from the registry; owner), Evidence(account_registry)
    hris.asset.snapshot    -> Asset(tags)
    hris.document          -> Evidence(document_type from front matter)

``shared`` is assigned only from the registry's explicit ``kind`` field, never inferred.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from ccp.connectors.base import SourceEvent, parse_ts
from ccp.normalize.entities import (
    AccessGrant,
    Asset,
    Entity,
    Evidence,
    MappingResult,
    Person,
    Vendor,
)
from ccp.types import AccountKind, EmploymentStatus, EvidenceType, GrantState

IDP_ASSET_ID = "hris:asset:identity_provider"


def person_id(employee_id: str) -> str:
    return f"hris:person:{employee_id}"


def account_person_id(account_name: str) -> str:
    return f"hris:account:{account_name}"


def _base(event: SourceEvent, entity_id: str) -> dict[str, Any]:
    return {
        "tenant_id": event.tenant_id,
        "entity_id": entity_id,
        "source_connector": "hris",
        "source_event_id": event.source_event_id,
        "observed_at": event.source_ts,
    }


def _status(value: str | None) -> EmploymentStatus:
    if value == "active":
        return EmploymentStatus.active
    if value == "terminated":
        return EmploymentStatus.terminated
    return EmploymentStatus.unknown


def _ts(value: Any) -> datetime | None:
    if not value:
        return None
    text = str(value)
    if len(text) == 10:  # a bare date
        text += "T00:00:00Z"
    return parse_ts(text)


def _kind(value: str | None) -> AccountKind:
    try:
        return AccountKind(value or "unknown")
    except ValueError:
        return AccountKind.unknown


def map_hris_event(event: SourceEvent) -> MappingResult:
    p = event.payload
    kind = event.event_type
    out: list[Entity] = []

    if kind == "hris.employee.snapshot":
        status = _status(p.get("status"))
        effective = (
            _ts(p.get("terminated_at"))
            if status is EmploymentStatus.terminated
            else _ts(p.get("hired_at"))
        )
        out.append(
            Person(
                **_base(event, person_id(p["employee_id"])),
                display_name=p["display_name"],
                emails=(p["work_email"],),
                handles={},
                employee_id=p["employee_id"],
                account_kind=AccountKind.human,
                employment_status=status,
                status_effective_at=effective,
            )
        )
        out.append(
            Evidence(
                **_base(event, f"hris:evidence:{p['employee_id']}:roster"),
                evidence_type=EvidenceType.hr_roster,
                title=f"HR roster: {p['display_name']}",
                text=(
                    f"{p['display_name']} ({p['employee_id']}), {p.get('title')}, "
                    f"{p.get('department')}, {p.get('employment_type')}, status {p.get('status')}, "
                    f"hired {p.get('hired_at')}, terminated {p.get('terminated_at') or 'n/a'}, "
                    f"email {p['work_email']}."
                ),
                subject_entity_id=person_id(p["employee_id"]),
                issued_at=event.source_ts,
                raw_ref=f"hris:employees/{p['employee_id']}",
            )
        )
        out.append(
            Vendor(
                **_base(event, "hris:vendor:hris"),
                name="HR and identity system",
                vendor_type="hris",
                data_categories=("employee_pii", "identities", "training_records"),
            )
        )
        return MappingResult(entities=tuple(out))

    if kind in ("hris.hire", "hris.termination", "hris.role_change"):
        details = p.get("details") or {}
        if kind == "hris.hire":
            status, effective = EmploymentStatus.active, event.source_ts
        elif kind == "hris.termination":
            status, effective = EmploymentStatus.terminated, event.source_ts
        else:
            status, effective = EmploymentStatus.unknown, None
        out.append(
            Person(
                **_base(event, person_id(p["employee_id"])),
                display_name=details.get("display_name"),  # partial observation: usually None
                employee_id=p["employee_id"],
                account_kind=AccountKind.human,
                employment_status=status,
                status_effective_at=effective,
            )
        )
        return MappingResult(entities=tuple(out))

    if kind == "hris.identity.snapshot":
        subject = (
            person_id(p["employee_id"])
            if p.get("employee_id")
            else account_person_id(p["email"].split("@")[0])
        )
        state = GrantState.active if p.get("status") == "active" else GrantState.revoked
        out.append(
            Asset(
                **_base(event, IDP_ASSET_ID),
                name="Identity provider (SSO)",
                asset_type="identity_provider",
                tags=("critical",),
                settings={},
            )
        )
        out.append(
            AccessGrant(
                **_base(event, f"hris:grant:{p['identity_id']}"),
                person_entity_id=subject,
                asset_entity_id=IDP_ASSET_ID,
                role="sso",
                state=state,
                state_effective_at=event.source_ts,
                attributes={
                    "identity_id": p["identity_id"],
                    "mfa_enabled": bool(p.get("mfa_enabled")),
                    "kind": p.get("kind"),
                    "email": p.get("email"),
                },
            )
        )
        out.append(
            Evidence(
                **_base(event, f"hris:evidence:{p['identity_id']}:mfa"),
                evidence_type=EvidenceType.identity_mfa_status,
                title=f"Identity MFA status: {p.get('email')}",
                text=(
                    f"Identity {p['identity_id']} for {p.get('email')} (kind {p.get('kind')}) is "
                    f"{p.get('status')}; MFA enabled: {bool(p.get('mfa_enabled'))}."
                ),
                subject_entity_id=subject,
                issued_at=event.source_ts,
                raw_ref=f"hris:identities/{p['identity_id']}",
            )
        )
        return MappingResult(entities=tuple(out))

    if kind == "hris.account.snapshot":
        out.append(
            Person(
                **_base(event, account_person_id(p["name"])),
                display_name=p["name"],
                handles={"account": p["name"]},
                account_kind=_kind(p.get("kind")),
                employment_status=EmploymentStatus.unknown,
                owner_employee_id=p.get("owner_employee_id"),
            )
        )
        out.append(
            Evidence(
                **_base(event, f"hris:evidence:{p['account_id']}:registry"),
                evidence_type=EvidenceType.account_registry,
                title=f"Account registry: {p['name']}",
                text=(
                    f"Account {p['name']} ({p.get('kind')}) on {', '.join(p.get('systems', []))}: "
                    f"purpose {p.get('purpose')}; owner employee id "
                    f"{p.get('owner_employee_id') or 'none recorded'}."
                ),
                subject_entity_id=account_person_id(p["name"]),
                issued_at=event.source_ts,
                raw_ref=f"hris:accounts/{p['account_id']}",
            )
        )
        return MappingResult(entities=tuple(out))

    if kind == "hris.asset.snapshot":
        out.append(
            Asset(
                **_base(event, f"hris:asset:{p['asset_id']}"),
                name=p["name"],
                asset_type=p.get("asset_type", "saas_app"),
                tags=tuple(p.get("tags", [])),
                settings={"owner_employee_id": p.get("owner_employee_id")},
            )
        )
        return MappingResult(entities=tuple(out))

    if kind == "hris.document":
        try:
            evidence_type = EvidenceType(p["document_type"])
        except ValueError:
            return MappingResult(
                guardrail_events=(f"unknown_document_type:{p.get('document_type')}",)
            )
        doc_subject = person_id(p["subject_employee_id"]) if p.get("subject_employee_id") else None
        out.append(
            Evidence(
                **_base(event, f"hris:evidence:doc:{p['filename']}"),
                evidence_type=evidence_type,
                title=str(p.get("title") or p["filename"]),
                text=p["text"],
                subject_entity_id=doc_subject,
                issued_at=parse_ts(str(p["issued_at"])),
                raw_ref=f"hris:documents/{p['filename']}",
            )
        )
        return MappingResult(entities=tuple(out))

    return MappingResult(guardrail_events=(f"unmapped_event_type:{kind}",))
