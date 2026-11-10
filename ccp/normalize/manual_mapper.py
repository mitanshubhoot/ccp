"""Manual upload SourceEvent -> exactly one Evidence, or none with a guardrail event. Pure.

The tenant is the event's tenant (which the connector took from the directory). Any tenant
named in the manifest entry or the document's front matter is ignored and recorded as a
guardrail event (spec.md S13). A PDF without a text layer is rejected here with
``pdf_no_text_layer`` rather than indexed as empty. A refused upload (a path outside the
tenant's folder, A-96) becomes an ``upload_refused`` guardrail event and no evidence.
"""

from __future__ import annotations

from ccp.connectors.base import SourceEvent, parse_ts
from ccp.normalize.entities import Evidence, MappingResult
from ccp.normalize.hris_mapper import person_id
from ccp.types import EvidenceType


def map_manual_event(event: SourceEvent) -> MappingResult:
    p = event.payload
    if event.event_type == "manual.refused":
        name = str(p.get("filename") or "")[:120]
        return MappingResult(guardrail_events=(f"upload_refused:{p.get('reason')}:{name}",))
    events: list[str] = []
    for source in ("manifest", "front_matter"):
        claim = (p.get(source) or {}).get("tenant")
        if claim is not None and str(claim) != event.tenant_id:
            events.append(f"upload_tenant_claim_ignored:{source}:{claim}")

    pdf = p.get("pdf")
    page_texts: tuple[str, ...] | None = None
    if isinstance(pdf, dict):
        if pdf.get("status") != "ok":
            events.append(f"pdf_{pdf.get('status', 'unreadable')}:{p.get('filename')}")
            return MappingResult(guardrail_events=tuple(events))
        page_texts = tuple(str(t) for t in pdf.get("pages", []))

    try:
        evidence_type = EvidenceType(p["evidence_type"])
    except (KeyError, ValueError):
        events.append(f"unknown_evidence_type:{p.get('evidence_type')}")
        return MappingResult(guardrail_events=tuple(events))

    subject = person_id(p["subject_employee_id"]) if p.get("subject_employee_id") else None
    evidence = Evidence(
        tenant_id=event.tenant_id,
        entity_id=f"manual:evidence:{p['filename']}:{p['sha256'][:12]}",
        source_connector="manual",
        source_event_id=event.source_event_id,
        observed_at=event.source_ts,
        evidence_type=evidence_type,
        title=str(p.get("title") or p["filename"]),
        text=str(p.get("text") or ""),
        subject_entity_id=subject,
        issued_at=parse_ts(str(p["issued_at"])) if p.get("issued_at") else None,
        raw_ref=f"manual:uploads/{p['filename']}",
        page_texts=page_texts,
        guardrail_events=tuple(events),
    )
    return MappingResult(entities=(evidence,), guardrail_events=tuple(events))
