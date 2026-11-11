"""LT3 (A-163): a security questionnaire is answered by a fixed pipeline, not an agent:
the question's tested control decides yes or no from its current verdicts, the evidence
gives checked facts, a model writes the prose, and code checks it. A person approves each
answer and sends; an approved answer is reused only while what it rested on is unchanged.

The defects: CCP had no questionnaire feature, and an approved answer kept saying yes
after the control it rested on failed or the document it quoted was replaced.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from pydantic import BaseModel

from ccp.compliance_state import writer as state_writer
from ccp.config import Settings
from ccp.evaluators import runner as evaluations
from ccp.evaluators.tier2 import Providers
from ccp.llm.client import StubLLMClient
from ccp.normalize import repository
from ccp.questionnaires import pipeline, send, store
from ccp.questionnaires.schema import AnswerProse
from ccp.retrieval import index
from ccp.retrieval.config import RetrievalConfig
from ccp.retrieval.embeddings import HashingEmbeddingClient
from ccp.storage.scoped import ScopedDB
from tests.support.pipeline import prepared_tenants

pytestmark = pytest.mark.db
NOW = datetime(2026, 9, 11, 12, tzinfo=UTC)
QUESTIONS = [
    {"id": "leavers", "text": "Is access removed for leavers after termination?"},
    {"id": "mfa", "text": "Does your security policy require multi-factor authentication?"},
    {"id": "backups", "text": "Are backups encrypted at rest?"},
]
_BLOCK = re.compile(r"<<EVIDENCE-\w+ chunk_id=(\S+) [^>]*>>\n(.*?)\n<<END-", re.DOTALL)


class _Model:
    """Facts: the first sentence of a fenced chunk that names multi-factor. Prose: the
    direction when given, else yes on facts; `contradict` answers yes whatever it is told."""

    def __init__(self, contradict: bool = False) -> None:
        self.contradict = contradict

    def __call__(self, schema: type[BaseModel], system: Any, user: str) -> BaseModel:
        if schema.__name__ == "FactsReading":
            asked = user.rsplit("Question:", 1)[-1].lower()
            for chunk_id, body in _BLOCK.findall(user) if "multi-factor" in asked else []:
                for sentence in re.split(r"(?<=\.)\s+", body):
                    if "multi-factor" in sentence.lower():
                        quote = " ".join(sentence.split())[:200]
                        return schema.model_validate(
                            {
                                "facts": [
                                    {
                                        "statement": "MFA is required",
                                        "chunk_id": chunk_id,
                                        "quote": quote,
                                    }
                                ]
                            }
                        )
            return schema.model_validate({"facts": []})
        given = re.search(r"Direction decided by CCP's tested controls: (\w+)", user)
        direction = given.group(1) if given else "none"
        if self.contradict:
            return AnswerProse(answer="yes", text="Yes, within 2 days.")
        if direction in ("yes", "no"):
            return AnswerProse(
                answer=direction, text=f"{direction.title()}, per our tested controls."
            )
        if "- MFA is required" in user:
            return AnswerProse(
                answer="yes", text="Yes. Our policy requires multi-factor authentication."
            )
        return AnswerProse(answer="insufficient_evidence", text="We have no evidence on this yet.")


@pytest.fixture(scope="module")
def world(base_settings: Settings) -> Iterator[tuple[Settings, ScopedDB]]:
    with prepared_tenants(base_settings, "ccp_questionnaires", ("acme",)) as (settings, dbs):
        acme = dbs["acme"]
        index.refresh(acme, settings, HashingEmbeddingClient(), contextual_enrichment=False)
        evaluations.enqueue_all(acme, settings, now=NOW, tests=("HR-1",))
        evaluations.drain(acme, settings, now=NOW)
        state_writer.drain(acme)  # the heads the direction is read from
        yield settings, acme


def _providers(model: _Model | None) -> Providers:
    llm = StubLLMClient(handler=model) if model is not None else None
    return Providers(HashingEmbeddingClient(), None, llm, RetrievalConfig())


def test_questions_map_to_controls_by_their_terms() -> None:
    assert pipeline.map_control(QUESTIONS[0]["text"]) == "CTL-ACCESS-REVOKE"
    assert pipeline.map_control(QUESTIONS[1]["text"]) == "CTL-MFA"
    assert pipeline.map_control(QUESTIONS[2]["text"]) is None
    assert pipeline.map_control("anything", "CTL-LOGGING") == "CTL-LOGGING"
    assert store.question_key("Are backups encrypted?") == store.question_key(
        "are  backups ENCRYPTED"
    )


def test_the_pipeline_drafts_a_person_approves_and_sends(world: tuple[Settings, ScopedDB]) -> None:
    settings, acme = world
    qid = store.create(acme, "Vendor review", QUESTIONS, "buyer@example.com")
    drafted = pipeline.answer_all(acme, settings, _providers(_Model()), qid)
    assert len(drafted) == 3
    answers = {a.question_id: a for a in store.answers_of(acme, qid)}
    leavers, mfa, backups = answers["leavers"], answers["mfa"], answers["backups"]
    assert leavers.direction == "no" and leavers.answer == "no" and leavers.verdict_ids
    assert leavers.stages["direction"]["counts"].get("fail", 0) >= 1
    assert mfa.control_id == "CTL-MFA" and mfa.answer == "yes" and mfa.checks_passed
    (pointer,) = mfa.pointers
    assert pointer["source_event_id"] and "multi-factor" in pointer["quote"].lower()
    assert backups.control_id is None and backups.answer == "insufficient_evidence"
    assert pipeline.answer_all(acme, settings, _providers(_Model()), qid) == []  # nothing new

    with pytest.raises(ValueError, match="not approved"):
        send.send(acme, settings, qid, "sam")
    store.approve(acme, leavers.answer_id, "sam")
    store.approve(acme, mfa.answer_id, "sam", "Yes. MFA is required for every human identity.")
    store.approve(acme, backups.answer_id, "sam")
    path = send.send(acme, settings, qid, "sam")
    record = json.loads(path.read_text(encoding="utf-8"))
    assert [a["question_id"] for a in record["answers"]] == ["leavers", "mfa", "backups"]
    assert record["answers"][1]["edited"] and record["answers"][1]["approved_by"] == "sam"
    assert all(a.status == "sent" for a in store.answers_of(acme, qid))
    with pytest.raises(ValueError, match="was sent"):
        send.send(acme, settings, qid, "sam")
    m = send.metrics(acme)
    assert m.by_kind["read"]["approved"] == 1 and m.by_kind["mapped"]["edited"] == 1


def test_a_draft_that_contradicts_its_control_needs_the_reviewers_own_text(
    world: tuple[Settings, ScopedDB],
) -> None:
    settings, acme = world
    asked = {"id": "revoke", "text": "How quickly do you revoke a terminated employee's access?"}
    qid = store.create(acme, "Contradiction", [asked])  # not the library's wording
    (answer_id,) = pipeline.answer_all(acme, settings, _providers(_Model(contradict=True)), qid)
    found = store.answer(acme, answer_id)
    assert found is not None and not found.checks_passed
    failed = {c["name"] for c in found.checks if not c["passed"]}
    assert failed == {"direction", "numbers"}  # the controls say no; "2 days" is in no fact
    with pytest.raises(ValueError, match="own text"):
        store.approve(acme, answer_id, "sam")
    store.approve(acme, answer_id, "sam", "No. One leaver still had access at our last check.")

    keyless = store.create(acme, "No key", [asked])
    # the library holds an approved answer to this question now, so it is reused
    (reused_id,) = pipeline.answer_all(acme, settings, _providers(None), keyless)
    reused = store.answer(acme, reused_id)
    assert reused is not None and reused.source == "library"


def test_an_approved_answer_goes_stale_when_its_document_changes(
    world: tuple[Settings, ScopedDB],
) -> None:
    settings, acme = world
    qid = store.create(acme, "Second buyer", [QUESTIONS[1]])
    (answer_id,) = pipeline.answer_all(acme, settings, _providers(_Model()), qid)
    reused = store.answer(acme, answer_id)
    assert reused is not None and reused.source == "library"  # approved before: reused
    store.approve(acme, answer_id, "sam")
    assert send.sweep(acme, qid) == 0
    entity = reused.pointers[0]["evidence_entity_id"]
    current = repository.evidence_version(acme, entity)
    assert current is not None
    newer = current.model_copy(
        update={
            "source_event_id": f"{current.source_event_id}:v2",
            "observed_at": current.observed_at + timedelta(days=1),
            "text": current.text + "\nRevised.",
        }
    )
    with acme.transaction() as cur:
        repository.insert(cur, newer)
    assert send.sweep(acme, qid) == 1
    stale = store.answer(acme, answer_id)
    assert stale is not None and stale.status == "stale"
    assert stale.status_note == f"evidence_changed:{entity}"
    hit, reasons = store.match(acme, QUESTIONS[1]["text"])
    assert hit is None and reasons == [f"evidence_changed:{entity}"]  # not reused any more
    with pytest.raises(ValueError, match="not approved"):
        send.send(acme, settings, qid, "sam")
    assert send.metrics(acme).stale >= 1


def test_without_a_model_a_decided_control_still_answers(world: tuple[Settings, ScopedDB]) -> None:
    settings, acme = world
    asked = [
        {"id": "offboard", "text": "Do you deprovision accounts during offboarding?"},
        {"id": "pentest", "text": "Is an external penetration test performed yearly?"},
    ]
    qid = store.create(acme, "Keyless", asked)
    pipeline.answer_all(acme, settings, _providers(None), qid)
    answers = {a.question_id: a for a in store.answers_of(acme, qid)}
    assert answers["offboard"].answer == "no" and answers["offboard"].text.startswith("No. ")
    assert answers["offboard"].model is None and answers["offboard"].verdict_ids
    assert answers["pentest"].answer == "insufficient_evidence"
