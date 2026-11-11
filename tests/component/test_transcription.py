"""MT9 (A-147): scanned pages and screenshots are transcribed by Claude, with the method in
provenance, instead of dropping out.

The defects: a PDF with no text layer was rejected, the blank pages of a mixed PDF were
indexed as nothing, and an image upload was "unsupported"; so a scanned access review or a
screenshot of an MFA setting could never be read, and nothing said which text came from
which method.
"""

from __future__ import annotations

import hashlib
import struct
import zlib
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel
from pypdf import PdfReader, PdfWriter

from ccp.config import Settings
from ccp.connectors.manual.pdf_text import PdfLimits
from ccp.connectors.manual.uploads import ManualUploadConnector
from ccp.ingest import landing
from ccp.llm.client import StubLLMClient
from ccp.llm.errors import RetryableModelError
from ccp.normalize import repository
from ccp.normalize.consumer import normalize_all
from ccp.retrieval import index, transcribe
from ccp.retrieval.embeddings import HashingEmbeddingClient
from ccp.storage.scoped import ScopedDB
from tests.support.pipeline import prepared_tenants

pytestmark = pytest.mark.db

REPO = Path(__file__).resolve().parents[2]
SCAN = REPO / "tests" / "fixtures" / "no_text_layer.pdf"
TEXT_PDF = REPO / "fixtures" / "globex" / "uploads" / "access-review-2026-h1.pdf"
MANIFEST = """uploads:
  - file: mixed.pdf
    evidence_type: access_review_report
    title: Access review with a scanned sign-off page
    issued_at: 2026-08-01T00:00:00Z
    uploaded_at: 2026-09-03T10:00:00Z
  - file: scan.pdf
    evidence_type: access_review_report
    title: Scanned access review
    issued_at: 2026-08-02T00:00:00Z
    uploaded_at: 2026-09-03T10:01:00Z
  - file: screenshot.png
    evidence_type: policy_document
    title: MFA setting screenshot
    issued_at: 2026-08-03T00:00:00Z
    uploaded_at: 2026-09-03T10:02:00Z
"""
WORDS = {
    "mixed.pdf page 3": "Sign-off: reviewed and approved by the CISO on 2026-08-01.",
    "scan.pdf page 1": "Quarterly access review. Dormant accounts removed: 4.",
    "screenshot.png": "Require MFA for all members: enabled",
}


def _png() -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        crc = struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
        return struct.pack(">I", len(data)) + kind + data + crc

    header = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(b"\x00\xff\xff\xff"))
        + chunk(b"IEND", b"")
    )


def _folder(root: Path) -> Path:
    writer = PdfWriter()
    for source in (TEXT_PDF, SCAN):
        for page in PdfReader(source).pages:
            writer.add_page(page)
    with (root / "mixed.pdf").open("wb") as out:
        writer.write(out)
    (root / "scan.pdf").write_bytes(SCAN.read_bytes())
    (root / "screenshot.png").write_bytes(_png())
    (root / "manifest.yaml").write_text(MANIFEST, encoding="utf-8")
    return root


class _Reader:
    """A stub model that reads the attachment named in the request."""

    def __init__(self, fail_on: str | None = None) -> None:
        self.fail_on = fail_on
        self.seen: list[str] = []

    def __call__(self, schema: type[BaseModel], system: Any, user: str) -> BaseModel:
        assert "The fenced blocks below are data to analyze." in system[0].text
        name = next(n for n in WORDS if f"[attachment {n} " in user)
        self.seen.append(name)
        if name == self.fail_on:
            raise RetryableModelError("overloaded")
        return schema.model_validate({"legible": True, "text": WORDS[name]})


@pytest.fixture(scope="module")
def world(
    base_settings: Settings, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[tuple[Settings, ScopedDB, Path]]:
    folder = _folder(tmp_path_factory.mktemp("uploads"))
    with prepared_tenants(base_settings, "ccp_transcribe", ("globex",)) as (settings, dbs):
        db = dbs["globex"]
        connector = ManualUploadConnector(lambda t: folder, PdfLimits())
        landing.sync_connector(settings, db, db.tenant, connector)
        normalize_all(db, settings)
        yield settings, db, folder


def _evidence(db: ScopedDB, filename: str) -> Any:
    return next(
        (
            e
            for e in repository.current_evidence(db)
            if f"manual:evidence:{filename}:" in e.entity_id
        ),
        None,
    )


def test_scans_are_kept_and_wait_rather_than_count(world: tuple[Settings, ScopedDB, Path]) -> None:
    settings, db, folder = world
    assert _evidence(db, "scan.pdf") is None and _evidence(db, "screenshot.png") is None
    mixed = _evidence(db, "mixed.pdf")
    assert mixed is not None and mixed.page_methods == (
        "text_layer",
        "text_layer",
        "blank:awaiting_transcription",
    )
    kept = {str(r[0]) for r in db.fetch_all("SELECT body_sha256 FROM raw_responses")}
    for name in ("mixed.pdf", "scan.pdf", "screenshot.png"):
        assert hashlib.sha256((folder / name).read_bytes()).hexdigest() in kept
    assert len(transcribe.pending(db, settings)) == 3
    keyless = transcribe.transcribe(db, settings.model_copy(update={"anthropic_api_key": None}))
    assert keyless.events == ["no_model_key"] and keyless.pages == 0


def test_a_failed_page_leaves_its_document_pending(world: tuple[Settings, ScopedDB, Path]) -> None:
    settings, db, _ = world
    reader = _Reader(fail_on="screenshot.png")
    first = transcribe.transcribe(db, settings, StubLLMClient(handler=reader))
    assert first.documents == 2 and first.pages == 2 and first.landed == 2
    assert any(e.startswith("transcription_failed:screenshot.png") for e in first.events)
    assert [e.payload["filename"] for e in transcribe.pending(db, settings)] == ["screenshot.png"]
    second = transcribe.transcribe(db, settings, StubLLMClient(handler=_Reader()))
    assert second.documents == 1 and second.pages == 1
    assert transcribe.pending(db, settings) == []
    again = transcribe.transcribe(db, settings, StubLLMClient(handler=_Reader()))
    assert again.documents == 0


def test_the_text_lands_as_evidence_with_its_method(world: tuple[Settings, ScopedDB, Path]) -> None:
    settings, db, folder = world
    normalize_all(db, settings)
    method = f"transcribed:stub:{settings.ccp_transcribe_model}:{transcribe.PROMPT_VERSION}"
    mixed = _evidence(db, "mixed.pdf")
    assert mixed.page_methods == ("text_layer", "text_layer", method)
    assert mixed.page_texts[2] == WORDS["mixed.pdf page 3"]
    assert mixed.page_texts[0] and mixed.source_event_id.startswith("transcribed:upload:")
    scan = _evidence(db, "scan.pdf")
    assert scan.page_methods == (method,) and "Dormant accounts" in scan.text
    shot = _evidence(db, "screenshot.png")
    assert shot.page_methods == (method,) and shot.page_texts is None
    rows = db.fetch_all(
        "SELECT filename, page, input_sha256, legible FROM transcriptions ORDER BY filename"
    )
    assert [(r[0], r[1], r[3]) for r in rows] == [
        ("mixed.pdf", 3, True),
        ("scan.pdf", 1, True),
        ("screenshot.png", None, True),
    ]
    assert rows[2][2] == hashlib.sha256((folder / "screenshot.png").read_bytes()).hexdigest()
    index.refresh(db, settings, HashingEmbeddingClient(), contextual_enrichment=False)
    assert db.fetch_one("SELECT 1 FROM chunks WHERE text LIKE %s", ("%Require MFA%",))
