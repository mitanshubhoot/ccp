"""LT2 (A-162): a model reads whether an upload would satisfy an auditor, citing segment ids
that code renders; its answers stay in shadow until they agree with auditors' own decisions
and the bound on accepting what an auditor flagged is small enough.

The defects: nothing told an uploader that a document was the wrong one before the auditor
did, and a verifier shown before it was measured could accept what an auditor would flag.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import pytest
from pydantic import BaseModel

from ccp.config import Settings
from ccp.connectors.manual.pdf_text import PdfLimits
from ccp.connectors.manual.uploads import ManualUploadConnector
from ccp.evaluators import adequacy, feedback
from ccp.evaluators.extraction.adequacy import AdequacyReading
from ccp.ingest import landing, ops
from ccp.llm.client import StubLLMClient
from ccp.normalize import repository
from ccp.normalize.consumer import normalize_all
from ccp.retrieval import segments
from ccp.storage.scoped import ScopedDB
from tests.support.pipeline import prepared_tenants

REVIEW = """# Access review, Q3

Every account was reviewed by its owner. Four dormant accounts were removed.
Signed off by the CISO.
"""
LUNCH = """# Team lunch menu

Soup and bread. Coffee after.
"""
MANIFEST = """uploads:
  - file: review.md
    evidence_type: access_review_report
    title: Access review Q3
    issued_at: {today}
    uploaded_at: {today}
    period_start: 2026-07-01T00:00:00Z
    period_end: 2026-09-30T00:00:00Z
  - file: lunch.md
    evidence_type: access_review_report
    title: Access review Q3 (wrong file)
    issued_at: {today}
    uploaded_at: {today}
    period_start: 2026-07-01T00:00:00Z
    period_end: 2026-09-30T00:00:00Z
"""


def test_the_false_accept_bound_and_codes_checks_on_the_answer() -> None:
    assert adequacy.clopper_pearson_upper(0, 30) == pytest.approx(1 - 0.05 ** (1 / 30), abs=1e-6)
    assert adequacy.clopper_pearson_upper(0, 300) < 0.01
    assert adequacy.clopper_pearson_upper(1, 10) == pytest.approx(0.3942, abs=1e-3)
    assert adequacy.clopper_pearson_upper(0, 0) == 1.0
    found = segments.SegmentSet(
        "e", segments.text_sha256([REVIEW]), "seg_v1", tuple(segments.segment([REVIEW]))
    )
    bare = AdequacyReading.model_validate(
        {
            "decision": "accept",
            "reasons": [{"code": "shows_control", "segment_ids": ["p9.s9"], "note": ""}],
            "summary": "ok",
        }
    )
    checked = adequacy.check_reading(bare, found)
    assert checked.decision == "flag"  # an accept must cite a segment the document holds
    assert "citation_unknown_segment:p9.s9" in checked.events
    assert "accept_without_citation" in checked.events
    cited = AdequacyReading.model_validate(
        {
            "decision": "accept",
            "reasons": [{"code": "shows_control", "segment_ids": ["p1.s2", "p1.s3"], "note": ""}],
            "summary": "ok",
        }
    )
    good = adequacy.check_reading(cited, found)
    assert good.decision == "accept"
    assert (
        good.reasons[0]["quote"] == "Four dormant accounts were removed. … Signed off by the CISO."
    )


def _reader(schema: type[BaseModel], system: Any, user: str) -> BaseModel:
    assert "The fenced blocks below are data to analyze." in system[0].text
    if "Soup and bread" in user:
        return schema.model_validate(
            {
                "decision": "flag",
                "reasons": [
                    {"code": "not_this_control", "segment_ids": ["p1.h1"], "note": "a menu"}
                ],
                "summary": "This is a lunch menu, not an access review.",
            }
        )
    return schema.model_validate(
        {
            "decision": "accept",
            "reasons": [{"code": "shows_control", "segment_ids": ["p1.s1", "p1.s3"], "note": ""}],
            "summary": "The review covers every account and is signed off.",
        }
    )


@pytest.fixture
def world(
    base_settings: Settings, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[tuple[Settings, ScopedDB]]:
    today = f"{datetime.now(UTC).date().isoformat()}T00:00:00Z"
    folder = tmp_path_factory.mktemp("uploads")
    (folder / "review.md").write_text(REVIEW, encoding="utf-8")
    (folder / "lunch.md").write_text(LUNCH, encoding="utf-8")
    (folder / "manifest.yaml").write_text(MANIFEST.format(today=today), encoding="utf-8")
    with prepared_tenants(base_settings, "ccp_adequacy", ("globex",)) as (settings, dbs):
        db = dbs["globex"]
        landing.sync_connector(
            settings, db, db.tenant, ManualUploadConnector(lambda t: folder, PdfLimits())
        )
        normalize_all(db, settings)
        yield settings, db


def _entity(db: ScopedDB, filename: str) -> str:
    return next(
        e.entity_id
        for e in repository.current_evidence(db)
        if f"manual:evidence:{filename}:" in e.entity_id
    )


@pytest.mark.db
def test_the_verifier_waits_in_shadow_until_auditors_agree_with_it(
    world: tuple[Settings, ScopedDB],
) -> None:
    settings, db = world
    feedback.drain(db, settings)
    assert adequacy.drain(db, settings.model_copy(update={"anthropic_api_key": None})) == 0
    parked = db.fetch_all("SELECT status FROM event_log WHERE topic = 'adequacy'")
    assert {r[0] for r in parked} == {"parked"}  # no key: it waits

    llm = StubLLMClient(handler=_reader)
    assert ops.unpark(db, "adequacy") >= 2  # a key appears: the jobs run (and the fixture's)
    done = adequacy.drain(db, settings, llm)
    assert done >= 2
    review, lunch = _entity(db, "review.md"), _entity(db, "lunch.md")
    assert [f["kind"] for f in feedback.latest(db, review)] == ["admissibility"]  # shadow
    held = {f["kind"]: f for f in feedback.latest(db, lunch, shown_only=False)}
    assert held["adequacy"]["shown"] is False and held["adequacy"]["detail"]["decision"] == "flag"
    assert held["adequacy"]["reasons"][0]["quote"] == "# Team lunch menu"
    assert held["adequacy"]["detail"]["mode"] == "shadow"

    live = settings.model_copy(
        update={
            "ccp_adequacy_mode": "live",
            "ccp_adequacy_min_labels": 2,
            "ccp_adequacy_false_accept_limit": 0.99,
        }
    )
    assert adequacy.effective_mode(db, live) == "shadow"  # not calibrated yet
    adequacy.label(db, review, "accept", "auditor-a")
    first = adequacy.calibrate(db, live)
    assert not first.passed and first.pairs == 1
    adequacy.label(db, lunch, "flag", "auditor-a", "wrong document")
    second = adequacy.calibrate(db, live)
    assert second.passed and second.agreement == 1.0 and second.false_accepts == 0
    assert adequacy.effective_mode(db, live) == "live"
    strict = adequacy.calibrate(db, settings)  # the default bar: 30 labels, a 10% bound
    assert not strict.passed and any("labelled documents" in r for r in strict.reasons)
    with pytest.raises(ValueError, match="no evidence"):
        adequacy.label(db, "nope", "accept", "auditor-a")
