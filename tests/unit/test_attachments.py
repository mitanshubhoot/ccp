"""MT9 (A-147): what the model is sent for a transcription, and what is logged of it."""

from __future__ import annotations

import base64
from pathlib import Path

from pypdf import PdfReader

from ccp.connectors.manual.pdf_text import blank_pages, image_type, split_pages
from ccp.llm.anthropic_client import _loggable, _user_content
from ccp.llm.client import Attachment
from ccp.normalize.manual_mapper import map_manual_event

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


def test_attachments_go_ahead_of_the_instruction_and_are_logged_by_digest() -> None:
    png = Attachment("image/png", b"\x89PNG\r\n\x1a\nrest", "shot.png")
    pdf = Attachment("application/pdf", b"%PDF-1.4 page", "scan.pdf page 1")
    content = _user_content("Transcribe it.", ["a document"], [png, pdf])
    assert isinstance(content, list)
    assert [b["type"] for b in content] == ["image", "document", "document", "text"]
    assert base64.b64decode(content[0]["source"]["data"]) == png.data
    assert content[1]["source"]["media_type"] == "application/pdf"
    assert content[2]["source"]["type"] == "text"
    logged = _loggable({"messages": content, "model": "m"}, [png, pdf])
    assert logged["messages"] == "redacted" and png.sha256 in logged["attachments"][0]
    assert _user_content("plain", []) == "plain"


def test_images_are_known_by_their_bytes_not_their_name() -> None:
    assert image_type(b"\x89PNG\r\n\x1a\n...") == "image/png"
    assert image_type(b"\xff\xd8\xff\xe0...") == "image/jpeg"
    assert image_type(b"RIFF\x00\x00\x00\x00WEBPVP8 ") == "image/webp"
    assert image_type(b"not an image") is None
    assert blank_pages(["text", "  ", "", "more"]) == [2, 3]


def test_a_blank_page_is_split_out_on_its_own() -> None:
    pages = split_pages((FIXTURES / "no_text_layer.pdf").read_bytes(), [1, 99])
    assert list(pages) == [1]
    from io import BytesIO

    assert len(PdfReader(BytesIO(pages[1])).pages) == 1


def test_a_scan_with_nothing_readable_gives_no_evidence() -> None:
    from datetime import UTC, datetime

    from ccp.connectors.base import SourceEvent

    def event(payload: dict[str, object]) -> SourceEvent:
        base = {"filename": "x.pdf", "sha256": "ab" * 32, "evidence_type": "access_review_report"}
        return SourceEvent(
            tenant_id="acme",
            connector="manual",
            source_event_id="upload:x",
            source_ts=datetime(2026, 9, 1, tzinfo=UTC),
            event_type="manual.document",
            payload={**base, **payload},
            schema_version="manual.v1",
        )

    scan = map_manual_event(
        event({"pdf": {"status": "no_text_layer", "pages": [""]}, "attachment": {"sha256": "x"}})
    )
    assert scan.entities == () and "awaiting_transcription:x.pdf" in scan.guardrail_events
    shot = map_manual_event(event({"image": {"media_type": "image/png"}, "text": ""}))
    assert shot.entities == () and "awaiting_transcription:x.pdf" in shot.guardrail_events
