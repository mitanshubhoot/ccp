"""Claude transcription of scanned pages and screenshots (spec.md A-147).

Pages with no text layer, in wholly scanned or mixed PDFs, and image uploads used to drop
out of the index. The upload connector now keeps their bytes in the raw zone and names them
in the upload's event; this job, run on a schedule or by `ccp evidence transcribe`:

    pending uploads (an attachment, not yet transcribed)
      -> the bytes from the raw zone, checked against the digest the event names
      -> one-page PDFs of the blank pages (split in the capped child), or the image as is
      -> Claude reads each one: word for word, [illegible] where it cannot, `legible` false
         for a page with nothing to read (prompt transcribe_v1)
      -> one `transcriptions` row per page: the input digest, model, prompt, legibility, cost
      -> a `manual.transcribed` raw event, landed like any other, that the manual mapper turns
         into a newer observation of the same evidence with each page's method recorded

A document is transcribed whole or not at all: a failed page leaves it pending for the next
run. Without a model key nothing runs and nothing is recorded.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field
from ulid import ULID

from ccp.config import Settings
from ccp.connectors.base import SourceEvent
from ccp.connectors.manual.pdf_text import split_pages
from ccp.ingest import landing, raw_zone
from ccp.llm.budget import BudgetTracker, RunBudget
from ccp.llm.client import Attachment, LLMClient, SystemBlock, try_build_client
from ccp.llm.errors import RetryableModelError, TerminalModelError
from ccp.llm.prompts import load_prompt
from ccp.storage.scoped import ScopedDB

PROMPT_VERSION = "transcribe_v1"


class Transcription(BaseModel):
    model_config = ConfigDict(extra="forbid")

    legible: bool
    text: str = Field(max_length=40_000)


@dataclass
class TranscribeSummary:
    documents: int = 0
    pages: int = 0
    illegible: int = 0
    landed: int = 0
    usd: float = 0.0
    events: list[str] = field(default_factory=list)


def pending(db: ScopedDB, settings: Settings) -> list[SourceEvent]:
    """Upload events that carry an attachment and have no transcription yet."""
    rows = db.fetch_all(
        "SELECT r.raw_path FROM raw_events r WHERE r.connector = 'manual'"
        " AND strpos(r.source_event_id, 'upload:') = 1"
        " AND NOT EXISTS (SELECT 1 FROM transcriptions t"
        "   WHERE t.source_event_id = r.source_event_id)"
        " ORDER BY r.source_ts, r.source_event_id"
    )
    events = [raw_zone.read_ref(str(r[0]), settings) for r in rows]
    return [e for e in events if isinstance(e.payload.get("attachment"), dict)]


def _bytes(db: ScopedDB, settings: Settings, digest: str) -> bytes | None:
    row = db.fetch_one(
        "SELECT ref FROM raw_responses WHERE connector = 'manual' AND body_sha256 = %s"
        " ORDER BY received_at LIMIT 1",
        (digest,),
    )
    if row is None:
        return None
    data = raw_zone.read_bytes(str(row[0]), settings)
    return data if hashlib.sha256(data).hexdigest() == digest else None


def _attachments(event: SourceEvent, data: bytes) -> dict[str, Attachment]:
    """Page number (or 'image') -> what the model reads."""
    p = event.payload
    name = str(p.get("filename"))
    pdf = p.get("pdf")
    if isinstance(pdf, dict):
        blanks = [int(n) for n in pdf.get("blank_pages") or []]
        return {
            str(n): Attachment("application/pdf", page, f"{name} page {n}")
            for n, page in split_pages(data, blanks).items()
        }
    media_type = str((p.get("attachment") or {}).get("media_type"))
    return {"image": Attachment(media_type, data, name)}


def _transcribe_one(
    llm: LLMClient,
    settings: Settings,
    tracker: BudgetTracker,
    system: list[SystemBlock],
    attachment: Attachment,
) -> tuple[Transcription, str, float]:
    result = llm.extract(
        schema=Transcription,
        system=system,
        user=f"Transcribe {attachment.name}.",
        model=settings.ccp_transcribe_model,
        tracker=tracker,
        max_tokens=8192,
        attachments=[attachment],
    )
    parsed: Transcription = result.parsed
    return parsed, result.model, result.usage.usd


def transcribe(db: ScopedDB, settings: Settings, llm: LLMClient | None = None) -> TranscribeSummary:
    summary = TranscribeSummary()
    client = llm if llm is not None else try_build_client(settings)
    if client is None:
        summary.events.append("no_model_key")
        return summary
    prompt = load_prompt(PROMPT_VERSION)
    system = [SystemBlock(prompt.text, cache=True)]
    tracker = BudgetTracker(RunBudget.from_settings(settings).lowered(max_seconds=3600))
    zone = raw_zone.open_zone(settings)
    budget = settings.ccp_transcribe_max_pages
    for event in pending(db, settings):
        name = str(event.payload.get("filename"))
        digest = str(event.payload["attachment"]["sha256"])
        data = _bytes(db, settings, digest)
        if data is None:
            summary.events.append(f"attachment_missing:{name}")
            continue
        try:
            items = _attachments(event, data)
        except ValueError as exc:
            summary.events.append(f"split_failed:{name}:{exc}"[:200])
            continue
        if len(items) > budget:
            summary.events.append(f"page_budget_reached:{name}")
            break
        done: dict[str, tuple[Transcription, str, float, Attachment]] = {}
        try:
            for key, attachment in items.items():
                parsed, model, usd = _transcribe_one(client, settings, tracker, system, attachment)
                done[key] = (parsed, model, usd, attachment)
        except (RetryableModelError, TerminalModelError) as exc:
            summary.events.append(f"transcription_failed:{name}:{type(exc).__name__}")
            continue  # whole or not at all: the document stays pending
        budget -= len(items)
        pages: dict[str, str] = {}
        methods: dict[str, str] = {}
        tenant = db.tenant.tenant_id
        with db.transaction() as cur:
            for key, (parsed, model, usd, attachment) in done.items():
                legible = parsed.legible and bool(parsed.text.strip())
                method = f"{'transcribed' if legible else 'illegible'}:{model}:{PROMPT_VERSION}"
                pages[key] = parsed.text if legible else ""
                methods[key] = method
                summary.illegible += 0 if legible else 1
                summary.usd += usd
                cur.execute(
                    "INSERT INTO transcriptions (tenant_id, transcription_id, source_event_id,"
                    " filename, page, input_sha256, model, prompt_version, legible,"
                    " text_sha256, chars, usd)"
                    " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                    (
                        tenant,
                        str(ULID()),
                        event.source_event_id,
                        name,
                        int(key) if key != "image" else None,
                        attachment.sha256,
                        model,
                        PROMPT_VERSION,
                        legible,
                        hashlib.sha256(pages[key].encode("utf-8")).hexdigest(),
                        len(pages[key]),
                        round(usd, 6),
                    ),
                )
        summary.documents += 1
        summary.pages += len(done)
        derived = _derived(event, pages, methods)
        landed, _ = landing.land_events(db, zone, [derived])
        summary.landed += landed
    return summary


def _derived(event: SourceEvent, pages: dict[str, str], methods: dict[str, str]) -> SourceEvent:
    payload: dict[str, Any] = {
        **event.payload,
        "transcribed": {
            "pages": pages,
            "methods": methods,
            "prompt_version": PROMPT_VERSION,
            "from_event": event.source_event_id,
        },
    }
    return SourceEvent(
        tenant_id=event.tenant_id,
        connector="manual",
        source_event_id=f"transcribed:{event.source_event_id}:{PROMPT_VERSION}",
        source_ts=datetime.now(UTC),
        event_type="manual.transcribed",
        payload=payload,
        schema_version=event.schema_version,
    )
