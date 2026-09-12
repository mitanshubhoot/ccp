"""Citation verification (S3), the pure rules over every field combination, and the computed
confidence with its forcing rules."""

from __future__ import annotations

import itertools
from datetime import UTC, datetime

import pytest

from ccp.evaluators import citations, confidence, rules
from ccp.evaluators.extraction import schema_for
from ccp.evaluators.extraction.base import Cited
from ccp.evaluators.extraction.gh4 import DisclosurePolicyFields
from ccp.evaluators.extraction.hr3 import TrainingRecordFields
from ccp.types import VerdictStatus

NOW = datetime(2026, 9, 11, tzinfo=UTC)
CHUNK = (
    "We acknowledge every report within 5 business days. Confirmed vulnerabilities receive a plan."
)
CHUNKS = {"c1": CHUNK, "c2": "Status: Completed\nCompletion date: 2026-03-14\nProvider: KnowBe4"}
GH4 = schema_for("DisclosurePolicyFields")
HR3 = schema_for("TrainingRecordFields")


def _gh4(**kw: object) -> DisclosurePolicyFields:
    base: dict[str, object] = {"policy_exists": True, "timeline_stated": True}
    base.update(kw)
    return DisclosurePolicyFields.model_validate(base)


def test_a_fabricated_chunk_id_is_dropped() -> None:  # S3 (a)
    parsed = _gh4(
        policy_exists_citation=Cited(chunk_id="not-retrieved", quote="anything"),
        timeline_stated_citation=Cited(chunk_id="c1", quote="within 5 business days"),
        timeline_text="within 5 business days",
        timeline_text_citation=Cited(chunk_id="c1", quote="within 5 business days"),
    )
    out = citations.verify_fields(parsed, CHUNKS, GH4)
    assert out.values["policy_exists"] is None and out.values["timeline_stated"] is True
    assert "citation_unknown_chunk:policy_exists:not-retrieved" in out.events
    assert out.citation_rate == pytest.approx(2 / 3) and out.coverage == 0.5


def test_a_quote_not_in_the_chunk_is_dropped() -> None:  # S3 (b)
    parsed = _gh4(
        policy_exists_citation=Cited(chunk_id="c1", quote="We acknowledge every report"),
        timeline_stated_citation=Cited(chunk_id="c1", quote="we respond within 1 hour"),
        timeline_text="within 1 hour",
        timeline_text_citation=Cited(chunk_id="c1", quote="we respond within 1 hour"),
    )
    out = citations.verify_fields(parsed, CHUNKS, GH4)
    assert out.values["timeline_stated"] is None and out.values["timeline_text"] is None
    assert any(e.startswith("citation_quote_not_found:timeline_stated") for e in out.events)
    assert [c.verified for c in out.citations] == [True, False, False]


def test_a_verdict_whose_only_citation_is_dropped_becomes_needs_review() -> None:  # S3 (c)
    parsed = _gh4(policy_exists_citation=Cited(chunk_id="c1", quote="nowhere in the chunk"))
    out = citations.verify_fields(parsed, CHUNKS, GH4)
    decision = rules.decide("gh4_disclosure_timeline", out.values, {}, NOW)
    assert decision.status is VerdictStatus.needs_review
    assert out.lost_required_citation


def test_token_containment_tolerates_a_dropped_comma_but_not_new_words() -> None:
    assert citations.quote_supported("acknowledge every report within 5 business days", CHUNK)
    assert citations.quote_supported(
        "We acknowledge every report within 5 business days Confirmed", CHUNK
    )
    assert not citations.quote_supported(
        "we escalate every report to the board within 5 days", CHUNK
    )
    assert not citations.quote_supported("", CHUNK)


def test_value_must_appear_in_its_quote_for_text_and_date_fields() -> None:
    assert citations.value_in_quote(
        "within 5 business days", "We acknowledge every report within 5 business days."
    )
    assert not citations.value_in_quote(
        "within 3 days", "We acknowledge every report within 5 business days."
    )
    assert citations.value_in_quote("2026-03-14", "Completion date: 2026-03-14")
    assert citations.value_in_quote(True, "anything") and citations.value_in_quote(None, "x")


def test_absent_values_need_no_citation_and_positive_claims_do() -> None:
    parsed = _gh4(policy_exists=False, timeline_stated=False)
    out = citations.verify_fields(parsed, CHUNKS, GH4)
    assert out.coverage == 1.0 and out.citation_rate == 1.0 and out.events == []
    uncited = _gh4(policy_exists=True, timeline_stated=False)
    out2 = citations.verify_fields(uncited, CHUNKS, GH4)
    assert out2.values["policy_exists"] is None and "uncited_field:policy_exists" in out2.events


def test_hr3_verification_keeps_the_cited_values() -> None:
    parsed = TrainingRecordFields(
        subject_name_in_document=True,
        subject_name_in_document_citation=Cited(chunk_id="c2", quote="Status: Completed"),
        completion_status="completed",
        completion_status_citation=Cited(chunk_id="c2", quote="Status: Completed"),
        completion_date="2026-03-14",
        completion_date_citation=Cited(chunk_id="c2", quote="Completion date: 2026-03-14"),
        provider="KnowBe4",
        provider_citation=Cited(chunk_id="c2", quote="Provider: KnowBe4"),
    )
    out = citations.verify_fields(parsed, CHUNKS, HR3)
    assert out.values["completion_date"] == "2026-03-14" and out.values["provider"] == "KnowBe4"
    assert out.coverage == 1.0 and out.citation_rate == 1.0


# -- rules over every field combination ------------------------------------------------------


@pytest.mark.parametrize(
    ("exists", "stated", "text"),
    list(
        itertools.product(
            (None, False, True), (None, False, True), (None, "within 5 business days")
        )
    ),
)
def test_gh4_rule_every_combination(
    exists: bool | None, stated: bool | None, text: str | None
) -> None:
    decision = rules.decide(
        "gh4_disclosure_timeline",
        {"policy_exists": exists, "timeline_stated": stated, "timeline_text": text},
        {},
        NOW,
    )
    if exists is None:
        expected = VerdictStatus.needs_review
    elif exists is False:
        expected = VerdictStatus.failed
    elif stated is None:
        expected = VerdictStatus.needs_review
    elif stated and text:
        expected = VerdictStatus.passed
    elif stated:
        expected = VerdictStatus.needs_review
    else:
        expected = VerdictStatus.partial
    assert decision.status is expected and decision.reasons


@pytest.mark.parametrize(
    ("theirs", "status", "date"),
    list(
        itertools.product(
            (None, False, True),
            (None, "unknown", "in_progress", "not_started", "completed"),
            (None, "2026-03-14", "2025-06-30", "not-a-date"),
        )
    ),
)
def test_hr3_rule_every_combination(
    theirs: bool | None, status: str | None, date: str | None
) -> None:
    decision = rules.decide(
        "hr3_training_current",
        {"subject_name_in_document": theirs, "completion_status": status, "completion_date": date},
        {"evidence_max_age_days": 365},
        NOW,
    )
    if theirs is None:
        expected = VerdictStatus.needs_review
    elif theirs is False:
        expected = VerdictStatus.failed
    elif status in (None, "unknown"):
        expected = VerdictStatus.needs_review
    elif status != "completed":
        expected = VerdictStatus.failed
    elif date in (None, "not-a-date"):
        expected = VerdictStatus.needs_review
    elif date == "2025-06-30":
        expected = VerdictStatus.partial
    else:
        expected = VerdictStatus.passed
    assert decision.status is expected


def test_unknown_rule_is_an_error() -> None:
    with pytest.raises(KeyError):
        rules.decide("nope", {}, {}, NOW)


# -- confidence ---------------------------------------------------------------------------------


def test_confidence_is_the_product_of_the_three_factors() -> None:
    c = confidence.compute(coverage=1.0, citation_rate=0.5, retrieval_quality=0.9)
    assert c.value == pytest.approx(0.45) and c.cap == 1.0 and c.events == []
    assert c.forced_status is None


def test_uncalibrated_retrieval_counts_as_0_60_with_an_event() -> None:
    c = confidence.compute(coverage=1.0, citation_rate=1.0, retrieval_quality=None)
    assert c.value == pytest.approx(0.6) and "retrieval_uncalibrated" in c.events


@pytest.mark.parametrize(
    ("kwargs", "cap", "forced", "event"),
    [
        ({"no_evidence": True}, 0.20, VerdictStatus.needs_review, "no_evidence_cap"),
        (
            {"unresolved_evidence_type": True},
            0.40,
            VerdictStatus.needs_review,
            "unresolved_evidence_cap",
        ),
        ({"injection": True}, 0.60, None, "injection_cap"),
    ],
)
def test_forcing_rules(
    kwargs: dict[str, bool], cap: float, forced: VerdictStatus | None, event: str
) -> None:
    c = confidence.compute(coverage=1.0, citation_rate=1.0, retrieval_quality=1.0, **kwargs)
    assert c.cap == cap and c.value <= cap and c.forced_status is forced and event in c.events


def test_provider_outage_caps_the_retrieval_factor() -> None:
    c = confidence.compute(
        coverage=1.0, citation_rate=1.0, retrieval_quality=0.95, provider_degraded=True
    )
    assert c.retrieval_quality == 0.6 and "retrieval_degraded_cap" in c.events
