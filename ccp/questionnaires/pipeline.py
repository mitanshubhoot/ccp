"""The questionnaire pipeline: one question, one fixed sequence of stages (spec.md A-163).

    1 mapping       code: the tested control the question is about, from the question's
                    own control id or the terms in ccp/knowledge/question_map.yaml
    2 library       code: an approved answer to the same question, reused only while
                    nothing it rested on has changed
    3 direction     code: where the question maps to a control, its current verdicts decide
                    yes (every subject passes), no (one fails), or nothing (pending, mixed)
    4 retrieval     code: hybrid search over the evidence types the control's tests read
    5 facts         model, tool-less: facts with verbatim quotes, each checked by code
                    against its chunk; a quote that is not there is dropped
    6 prose         model, tool-less: the answer and two to four sentences, from the
                    direction and the checked facts only
    7 checks        code: the answer follows the direction; every number in the prose
                    appears in a fact; a yes, no or partial without a direction rests on a
                    fact

No stage is an agent, and no stage sends anything: a person approves each answer (with or
without an edit) and a person sends the questionnaire. Without a model key a decided
direction still gives a code-written answer, and an undecided one says so.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from ccp.compliance_state import autonomy
from ccp.config import Settings
from ccp.evaluators import citations
from ccp.evaluators.extraction import schema_block
from ccp.evaluators.tier2 import Providers
from ccp.knowledge import graph as knowledge
from ccp.knowledge.graph import KNOWLEDGE_DIR
from ccp.llm.budget import BudgetTracker, RunBudget
from ccp.llm.client import SystemBlock
from ccp.llm.prompts import load_prompt
from ccp.normalize.repository import evidence_version
from ccp.questionnaires import store
from ccp.questionnaires.schema import FACTS_PROMPT, PROSE_PROMPT, AnswerProse, FactsReading
from ccp.retrieval import hybrid
from ccp.security import prompt_guard
from ccp.storage.scoped import ScopedDB
from ccp.tasks.planners import Facts

QUESTION_MAP = KNOWLEDGE_DIR / "question_map.yaml"
TOP_K = 6
_NUMBER = re.compile(r"\b\d+(?:[.,]\d+)?\b")


@lru_cache(maxsize=2)
def _terms(path: Path = QUESTION_MAP) -> dict[str, tuple[str, ...]]:
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return {str(k): tuple(str(t).lower() for t in v or ()) for k, v in raw.items()}


def map_control(question: str, given: str | None = None) -> str | None:
    """The control the question names, else the one whose terms it contains most."""
    graph = knowledge.load()
    if given and given in graph.controls:
        return given
    text = f" {question.lower()} "
    scores = {
        control: sum(1 for t in terms if t in text)
        for control, terms in _terms().items()
        if control in graph.controls
    }
    best = max(scores.values(), default=0)
    winners = [c for c, s in scores.items() if s == best]
    return winners[0] if best > 0 and len(winners) == 1 else None


def direction_of(facts: Facts, control_id: str) -> tuple[str, list[str], dict[str, int]]:
    """yes, no, pending or undecided from the control's tests' current states, with the
    verdicts that decided it. The same reading as the questionnaire task (A-150)."""
    tests = list(facts.graph.controls[control_id].tests)
    states = [s for t in tests for s in facts.states(t)]
    counts: dict[str, int] = {}
    for s in states:
        counts[s.state] = counts.get(s.state, 0) + 1
    decided = sorted({str(s.verdict_id) for s in states if s.verdict_id})
    if any(s.state == "fail" for s in states):
        return "no", decided, counts
    if any(s.state == "pending" for s in states):
        return "pending", [], counts
    if states and all(s.state == "pass" for s in states):
        return "yes", decided, counts
    return "undecided", decided, counts


def evidence_types(control_id: str | None, given: str | None) -> list[str]:
    """Policy documents always (a questionnaire mostly asks what the policies say), plus
    what the control's tests read, or the type the question names."""
    if given:
        return sorted({"policy_document", given})
    graph = knowledge.load()
    found = {"policy_document"}
    if control_id is not None:
        found |= {
            e.value for t in graph.controls[control_id].tests for e in graph.tests[t].evidence_types
        }
    return sorted(found)


def retrieve(
    db: ScopedDB, providers: Providers, question: str, types: list[str]
) -> list[dict[str, Any]]:
    chunks: list[dict[str, Any]] = []
    for evidence_type in types:
        result = hybrid.retrieve(
            db,
            providers.embedder,
            question,
            evidence_type=evidence_type,
            reranker=providers.reranker,
            config=providers.retrieval,
            top_k=TOP_K,
        )
        for c in result.chunks:
            chunks.append(
                {
                    "chunk_id": c.chunk_id,
                    "evidence_entity_id": c.evidence_entity_id,
                    "evidence_type": evidence_type,
                    "text": c.text,
                    "page": c.page,
                    "score": c.rerank_score if c.rerank_score is not None else c.fused_score,
                }
            )
    chunks.sort(key=lambda c: c["score"], reverse=True)
    return chunks[:TOP_K]


def _framed(chunks: list[dict[str, Any]], nonce: str) -> str:
    return prompt_guard.frame_evidence(
        [
            prompt_guard.FramedChunk(
                chunk_id=c["chunk_id"],
                text=c["text"],
                evidence_type=c["evidence_type"],
                page=c.get("page"),
            )
            for c in chunks
        ],
        nonce,
    ).text


def check_prose(
    prose: AnswerProse, direction: str, facts: list[dict[str, Any]], question: str
) -> list[dict[str, Any]]:
    """Code's checks on the drafted answer, each passed or failed with what failed."""
    checks: list[dict[str, Any]] = []
    follows = direction not in ("yes", "no") or prose.answer == direction
    checks.append(
        {
            "name": "direction",
            "passed": follows,
            "detail": "" if follows else f"the controls say {direction}, the draft {prose.answer}",
        }
    )
    support = " ".join([question, *(f"{f['statement']} {f['quote']}" for f in facts)])
    loose = [n for n in _NUMBER.findall(prose.text) if n not in support]
    checks.append({"name": "numbers", "passed": not loose, "detail": ", ".join(loose)})
    grounded = (
        direction in ("yes", "no")
        or prose.answer in ("insufficient_evidence", "not_applicable")
        or bool(facts)
    )
    checks.append(
        {
            "name": "grounded",
            "passed": grounded,
            "detail": "" if grounded else f"{prose.answer} with no checked fact",
        }
    )
    return checks


@dataclass
class Drafted:
    answer: str
    text: str
    facts: list[dict[str, Any]]
    checks: list[dict[str, Any]]
    model: str | None
    usd: float


def _code_written(direction: str, control_id: str | None) -> Drafted:
    """No model: a decided direction still answers; anything else says it is open."""
    if direction in ("yes", "no") and control_id is not None:
        title = knowledge.load().controls[control_id].title
        verb = "passes" if direction == "yes" else "does not pass"
        word = "Yes" if direction == "yes" else "No"
        text = f'{word}. CCP\'s tested control "{title}" {verb} its tests as of today.'
        return Drafted(
            direction, text, [], [{"name": "direction", "passed": True, "detail": ""}], None, 0.0
        )
    return Drafted(
        "insufficient_evidence",
        "No tested control or checked evidence decides this question yet.",
        [],
        [{"name": "no_model", "passed": True, "detail": "drafted without a model"}],
        None,
        0.0,
    )


def _drafted(
    settings: Settings,
    providers: Providers,
    question: str,
    direction: str,
    chunks: list[dict[str, Any]],
) -> Drafted:
    llm = providers.llm
    assert llm is not None
    tracker = BudgetTracker(
        RunBudget.from_settings(settings).lowered(max_seconds=180, max_usd=0.50)
    )
    nonce = prompt_guard.new_nonce()
    found = llm.extract(
        schema=FactsReading,
        system=[
            SystemBlock(load_prompt(FACTS_PROMPT).text),
            SystemBlock(schema_block(FactsReading), cache=True),
        ],
        user=f"{prompt_guard.fence_declaration(nonce)}\n\nQuestion: {question}",
        model=settings.ccp_questionnaire_model,
        tracker=tracker,
        documents=[_framed(chunks, nonce)] if chunks else ["(no evidence was found)"],
    )
    by_id = {c["chunk_id"]: c for c in chunks}
    facts = [
        {**f.model_dump(), "evidence_entity_id": by_id[f.chunk_id]["evidence_entity_id"]}
        for f in found.parsed.facts
        if f.chunk_id in by_id and citations.quote_supported(f.quote, by_id[f.chunk_id]["text"])
    ]
    given = direction if direction in ("yes", "no") else "none: decide from the facts"
    facts_nonce = prompt_guard.new_nonce()
    listed = "\n".join(f'- {f["statement"]} ("{f["quote"]}")' for f in facts)
    fenced_facts = prompt_guard.frame_evidence(  # quotes are evidence: a document block
        [
            prompt_guard.FramedChunk(
                chunk_id="facts", text=listed or "(no checked facts)", evidence_type="facts"
            )
        ],
        facts_nonce,
    ).text
    prose = llm.extract(
        schema=AnswerProse,
        system=[
            SystemBlock(load_prompt(PROSE_PROMPT).text),
            SystemBlock(schema_block(AnswerProse), cache=True),
        ],
        user=(
            f"{prompt_guard.fence_declaration(facts_nonce)}\n\nQuestion: {question}\n"
            f"Direction decided by CCP's tested controls: {given}\n"
            "The checked facts are in the fenced block."
        ),
        model=settings.ccp_questionnaire_model,
        tracker=tracker,
        documents=[fenced_facts],
    )
    parsed: AnswerProse = prose.parsed
    checks = check_prose(parsed, direction, facts, question)
    dropped = len(found.parsed.facts) - len(facts)
    if dropped:
        checks.append(
            {"name": "quotes", "passed": True, "detail": f"{dropped} unsupported fact(s) dropped"}
        )
    return Drafted(
        parsed.answer, parsed.text, facts, checks, prose.model, found.usage.usd + prose.usage.usd
    )


def answer_question(
    db: ScopedDB,
    settings: Settings,
    providers: Providers,
    questionnaire_id: str,
    q: dict[str, Any],
    facts: Facts | None = None,
) -> str:
    """Run the stages for one question and store the draft for a person."""
    question = str(q["text"]).strip()
    control_id = map_control(question, q.get("control_id"))
    stages: dict[str, Any] = {"mapping": {"control_id": control_id, "given": q.get("control_id")}}
    hit, stale = store.match(db, question)
    stages["library"] = {"hit": hit.library_id if hit else None, "stale": stale}
    if hit is not None:
        answer_id = store.record_answer(
            db,
            questionnaire_id=questionnaire_id,
            question_id=str(q["id"]),
            question=question,
            control_id=control_id,
            source="library",
            direction="library",
            answer=hit.answer,
            text=hit.text,
            stages=stages,
            checks=[{"name": "library_fresh", "passed": True, "detail": hit.library_id}],
            pointers=hit.pointers,
            verdict_ids=hit.verdict_ids,
            library_id=hit.library_id,
        )
        if autonomy.decide(db, "questionnaire", "library_answer", answer_id) == "auto":
            store.approve(db, answer_id, "ccp:auto")  # the level and snapshot are recorded
        return answer_id
    read = facts or Facts.read(db, datetime.now(UTC))
    direction, verdict_ids, counts = (
        direction_of(read, control_id) if control_id else ("unmapped", [], {})
    )
    stages["direction"] = {"direction": direction, "verdict_ids": verdict_ids, "counts": counts}
    types = evidence_types(control_id, q.get("evidence_type"))
    chunks = retrieve(db, providers, question, types)
    stages["retrieval"] = {"types": types, "chunk_ids": [c["chunk_id"] for c in chunks]}
    drafted = (
        _drafted(settings, providers, question, direction, chunks)
        if providers.llm is not None
        else _code_written(direction, control_id)
    )
    stages["facts"] = drafted.facts
    pointers = []
    for f in drafted.facts:
        version = evidence_version(db, f["evidence_entity_id"])
        pointers.append(
            {
                "chunk_id": f["chunk_id"],
                "evidence_entity_id": f["evidence_entity_id"],
                "source_event_id": version.source_event_id if version else None,
                "quote": f["quote"],
            }
        )
    return store.record_answer(
        db,
        questionnaire_id=questionnaire_id,
        question_id=str(q["id"]),
        question=question,
        control_id=control_id,
        source="pipeline",
        direction=direction,
        answer=drafted.answer,
        text=drafted.text,
        stages=stages,
        checks=drafted.checks,
        pointers=pointers,
        verdict_ids=verdict_ids if direction in ("yes", "no") else [],
        model=drafted.model,
        usd=drafted.usd,
    )


def answer_all(
    db: ScopedDB, settings: Settings, providers: Providers, questionnaire_id: str
) -> list[str]:
    """Draft every question that has no live answer yet (a rejected or stale one is
    drafted again)."""
    found = store.get(db, questionnaire_id)
    if found is None:
        raise ValueError(f"no questionnaire {questionnaire_id!r}")
    live = {
        a.question_id
        for a in store.answers_of(db, questionnaire_id)
        if a.status in ("draft", "approved", "sent")
    }
    facts = Facts.read(db, datetime.now(UTC))
    return [
        answer_question(db, settings, providers, questionnaire_id, q, facts)
        for q in found["questions"]
        if str(q["id"]) not in live
    ]
