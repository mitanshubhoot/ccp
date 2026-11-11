"""Manual upload SourceEvent -> exactly one Evidence, or none with a guardrail event. Pure.

The tenant is the event's tenant (which the connector took from the directory). Any tenant
named in the manifest entry or the document's front matter is ignored and recorded as a
guardrail event (spec.md S13). A PDF without a text layer is rejected here with
``pdf_no_text_layer`` rather than indexed as empty. A refused upload (a path outside the
tenant's folder, A-96) becomes an ``upload_refused`` guardrail event and no evidence.

Scans (A-147). A PDF with no text layer and an image upload give no evidence until they are
transcribed (``awaiting_transcription``), so nothing counts a document nobody has read. A
PDF with some blank pages lands with those pages marked ``blank:awaiting_transcription``.
A ``manual.transcribed`` event, landed by the transcription job, maps to a newer observation
of the same evidence entity: the blank pages filled, and each page's method recorded
(``text_layer``, ``transcribed:<model>:<prompt>``, or ``illegible:<model>:<prompt>``).
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
    page_methods: tuple[str, ...] | None = None
    text = str(p.get("text") or "")
    transcribed = p.get("transcribed") if event.event_type == "manual.transcribed" else None
    if isinstance(transcribed, dict):
        methods = {str(k): str(v) for k, v in (transcribed.get("methods") or {}).items()}
        texts = {str(k): str(v) for k, v in (transcribed.get("pages") or {}).items()}
        if isinstance(pdf, dict):
            pages = [str(t) for t in pdf.get("pages", [])]
            page_texts = tuple(texts.get(str(n), t) for n, t in enumerate(pages, start=1))
            page_methods = tuple(
                methods.get(str(n), "text_layer") for n in range(1, len(pages) + 1)
            )
            text = "\n\n".join(page_texts)
        else:  # an image: one transcribed text
            text = texts.get("image", "")
            page_methods = (methods.get("image", "illegible"),)
        if not text.strip():
            events.append(f"transcription_illegible:{p.get('filename')}")
            return MappingResult(guardrail_events=tuple(events))
    elif isinstance(pdf, dict):
        status = pdf.get("status")
        if status != "ok":
            events.append(f"pdf_{status or 'unreadable'}:{p.get('filename')}")
            if status == "no_text_layer" and p.get("attachment"):
                events.append(f"awaiting_transcription:{p.get('filename')}")
            return MappingResult(guardrail_events=tuple(events))
        page_texts = tuple(str(t) for t in pdf.get("pages", []))
        page_methods = tuple(
            "text_layer" if t.strip() else "blank:awaiting_transcription" for t in page_texts
        )
    elif isinstance(p.get("image"), dict):
        events.append(f"awaiting_transcription:{p.get('filename')}")
        return MappingResult(guardrail_events=tuple(events))
    else:
        page_methods = ("markdown",)

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
        text=text,
        subject_entity_id=subject,
        issued_at=parse_ts(str(p["issued_at"])) if p.get("issued_at") else None,
        raw_ref=f"manual:uploads/{p['filename']}",
        page_texts=page_texts,
        guardrail_events=tuple(events),
        page_methods=page_methods,
    )
    return MappingResult(entities=(evidence,), guardrail_events=tuple(events))
