"""LT2 (A-161): every upload is segmented and checked by code seconds after it lands, and
a scan or screenshot with a blocking reason is not counted until a new upload fixes it.

The defects: an uploader heard nothing until an evaluation ran, a screenshot of the wrong
day counted as evidence once a model had transcribed it, and a model's quote could not be
traced to a stable span of the document.
"""

from __future__ import annotations

import struct
import zlib
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import pytest
from pydantic import BaseModel

from ccp.config import Settings
from ccp.connectors.manual.pdf_text import PdfLimits
from ccp.connectors.manual.uploads import ManualUploadConnector
from ccp.evaluators import feedback
from ccp.ingest import landing
from ccp.knowledge import admissibility
from ccp.llm.client import StubLLMClient
from ccp.normalize import repository
from ccp.normalize.consumer import normalize_all
from ccp.retrieval import index, segments, transcribe
from ccp.retrieval.embeddings import HashingEmbeddingClient
from ccp.storage.scoped import ScopedDB
from tests.support.pipeline import prepared_tenants

NOW = datetime(2026, 9, 10, tzinfo=UTC)
PAGE = """# Access review, Q3

Every account was reviewed by its owner. Four dormant accounts were removed.
The review closed on 2026-09-01.

| System | Owner | Removed |
|--------|-------|---------|
| GitHub | Ana   | 2       |
| Okta   | Ben   | 2       |
"""


# -- segments ----------------------------------------------------------------------------------


def test_segments_have_stable_ids_and_offsets_into_the_page() -> None:
    found = segments.segment([PAGE, "Signed by the CISO. Approved."])
    ids = [s.segment_id for s in found]
    assert ids[:4] == ["p1.h1", "p1.s1", "p1.s2", "p1.s3"]
    assert "p1.t1.r0.c0" in ids and "p1.t1.r2.c1" in ids and "p2.s2" in ids
    for s in found:
        page = PAGE if s.page == 1 else "Signed by the CISO. Approved."
        assert page[s.start : s.end] == s.text
    cell = next(s for s in found if s.segment_id == "p1.t1.r2.c1")
    assert cell.text == "Ben" and cell.kind == "cell"
    assert segments.segment([PAGE]) == segments.segment([PAGE])[: len(segments.segment([PAGE]))]
    stored = segments.SegmentSet("e1", segments.text_sha256([PAGE]), "seg_v1", tuple(found))
    rendered = segments.render(stored, ["p1.s2", "p1.t1.r1.c0", "p9.s1"])
    assert rendered.quote == "Four dormant accounts were removed. … GitHub"
    assert rendered.unknown == ("p9.s1",) and rendered.pointers[0]["page"] == 1
    doc = {"buckets": [{"name": "a/b", "blocks": {"acl": True}}]}
    assert segments.resolve_pointer(doc, "/buckets/0/blocks/acl") is True
    with pytest.raises(KeyError):
        segments.resolve_pointer(doc, "/buckets/1")


# -- admissibility -----------------------------------------------------------------------------


def _check(**kw: Any) -> admissibility.Admissibility:
    base: dict[str, Any] = {
        "evidence_type": "access_review_report",
        "source_connector": "manual",
        "issued_at": datetime(2026, 9, 1, tzinfo=UTC),
        "text": "",
        "page_methods": ("text_layer",),
        "subject_entity_id": None,
        "manifest": {"period_start": "2026-07-01", "period_end": "2026-09-30"},
        "front_matter": None,
        "now": NOW,
    }
    return admissibility.check(**{**base, **kw})


def _codes(result: admissibility.Admissibility) -> list[str]:
    return [r.code for r in result.reasons]


def test_code_checks_date_period_subject_and_issuer() -> None:
    assert _check().admissible and _check().badge == "customer_attested"
    assert _codes(_check(issued_at=None)) == ["no_issue_date"]
    assert _codes(_check(issued_at=datetime(2026, 1, 1, tzinfo=UTC))) == ["too_old"]
    assert _codes(_check(issued_at=datetime(2026, 12, 1, tzinfo=UTC))) == ["issued_in_future"]
    assert _codes(_check(manifest={})) == ["no_period"]
    inverted = {"period_start": "2026-09-30", "period_end": "2026-07-01"}
    assert _codes(_check(manifest=inverted)) == ["period_inverted"]
    shot = _check(page_methods=("transcribed:stub:m:v1",), image=True, text="as of 2026-09-02")
    assert "one_moment" in _codes(shot) and not shot.admissible  # a screenshot is one moment
    training = _check(evidence_type="training_record", manifest={"subject_employee_id": "E1"})
    assert _codes(training) == ["subject_unresolved"]
    policy = _check(evidence_type="policy_document")
    assert policy.admissible and _codes(policy) == ["no_issuer"]  # a warning only
    signed = _check(evidence_type="policy_document", front_matter={"approved_by": "CISO"})
    assert _codes(signed) == []
    observed = _check(evidence_type="policy_document", source_connector="github")
    assert observed.badge == "system_observed"


def test_a_screenshot_is_held_to_the_date_visible_in_it() -> None:
    kw: dict[str, Any] = {
        "evidence_type": "policy_document",
        "page_methods": ("transcribed:stub:m:v1",),
        "image": True,
        "front_matter": {"approved_by": "CISO"},
    }
    fits = _check(text="MFA required: on. Checked Aug 30, 2026", **kw)
    assert fits.admissible and _codes(fits) == ["customer_attested_image"]
    stale = _check(text="MFA required: on. Checked 2025-01-05", **kw)
    assert not stale.admissible and "date_mismatch" in _codes(stale)
    blind = _check(text="MFA required: on", **kw)
    assert blind.admissible and "no_visible_date" in _codes(blind)


# -- end to end ----------------------------------------------------------------------------------


MANIFEST = """uploads:
  - file: mfa-today.png
    evidence_type: policy_document
    title: MFA setting, this week
    issued_at: {today}
    uploaded_at: {today}
    issuer: IT
  - file: mfa-old.png
    evidence_type: policy_document
    title: MFA setting, stated as this week
    issued_at: {today}
    uploaded_at: {today}
    issuer: IT
  - file: review.md
    evidence_type: access_review_report
    title: Access review Q3
    issued_at: {today}
    uploaded_at: {today}
"""


def _png(seed: int) -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        crc = struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
        return struct.pack(">I", len(data)) + kind + data + crc

    header = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
    pixel = zlib.compress(bytes([0, seed, seed, seed]))
    return (
        b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", pixel) + chunk(b"IEND", b"")
    )


class _Reader:
    def __init__(self, today: str) -> None:
        self.words = {
            "mfa-today.png": f"Require MFA for all members: enabled. Last changed {today}",
            "mfa-old.png": "Require MFA for all members: enabled. Last changed 2025-01-05",
        }

    def __call__(self, schema: type[BaseModel], system: Any, user: str) -> BaseModel:
        name = next(n for n in self.words if f"[attachment {n} " in user)
        return schema.model_validate({"legible": True, "text": self.words[name]})


@pytest.fixture(scope="module")
def world(
    base_settings: Settings, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[tuple[Settings, ScopedDB]]:
    today = datetime.now(UTC).date().isoformat()
    folder = tmp_path_factory.mktemp("uploads")
    (folder / "mfa-today.png").write_bytes(_png(1))
    (folder / "mfa-old.png").write_bytes(_png(2))
    (folder / "review.md").write_text(PAGE, encoding="utf-8")
    (folder / "manifest.yaml").write_text(
        MANIFEST.format(today=f"{today}T00:00:00Z"), encoding="utf-8"
    )
    with prepared_tenants(base_settings, "ccp_feedback", ("globex",)) as (settings, dbs):
        db = dbs["globex"]
        connector = ManualUploadConnector(lambda t: folder, PdfLimits())
        landing.sync_connector(settings, db, db.tenant, connector)
        normalize_all(db, settings)
        transcribe.transcribe(db, settings, StubLLMClient(handler=_Reader(today)))
        normalize_all(db, settings)
        yield settings, db


def _entity(db: ScopedDB, filename: str) -> str:
    return next(
        e.entity_id
        for e in repository.current_evidence(db)
        if f"manual:evidence:{filename}:" in e.entity_id
    )


@pytest.mark.db
def test_every_upload_gets_feedback_and_a_wrong_day_screenshot_is_not_counted(
    world: tuple[Settings, ScopedDB],
) -> None:
    settings, db = world
    given = feedback.drain(db, settings)
    assert given >= 3
    review = feedback.latest(db, _entity(db, "review.md"))
    (row,) = review
    assert row["admissible"] is False and [r["code"] for r in row["reasons"]] == ["no_period"]
    assert row["badge"] == "customer_attested" and row["seconds_after_landing"] is not None
    assert row["detail"]["segments"] > 5
    stored = db.fetch_one(
        "SELECT count(*) FROM segment_sets WHERE evidence_entity_id = %s",
        (_entity(db, "review.md"),),
    )
    assert stored is not None and stored[0] >= 1
    old = feedback.latest(db, _entity(db, "mfa-old.png"))
    assert [r["code"] for r in old[-1]["reasons"] if r["severity"] == "block"] == ["date_mismatch"]
    assert feedback.drain(db, settings) == 0  # once per version

    summary = index.refresh(db, settings, HashingEmbeddingClient(), contextual_enrichment=False)
    assert f"inadmissible:{_entity(db, 'mfa-old.png')}" in summary.events
    texts = [r[0] for r in db.fetch_all("SELECT text FROM chunks")]
    assert any("Last changed" in t and "2025" not in t for t in texts)
    assert not any("2025-01-05" in t for t in texts)  # the stale screenshot is not counted
    assert any("dormant accounts" in t.lower() for t in texts)  # a text upload still counts
