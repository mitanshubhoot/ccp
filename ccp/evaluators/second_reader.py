"""A second reader on every AI-derived PASS (spec.md A-143).

A tier 2 or tier 3 PASS rests on fields a model extracted and a rule decided. Before it
stands, a second model (`CCP_SECOND_READER_MODEL`, Sonnet 5.5 by default, not the tier 2
extractor) reads the field values beside the quotes code already verified, and says whether
each quote supports its value. It decides nothing: when it does not agree, the PASS becomes
needs_review with `second_reader_disagreed` and its reason, and a person looks. A FAIL, a
needs_review or a tier 1 verdict is not read twice: the second reader exists to stop a false
PASS, the costly error (6.6). The quotes reach it as documents, never in the instruction
(A-98), and its answer is kept on the verdict for replay.
"""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, Field

from ccp.config import Settings
from ccp.evaluators import budget_gate
from ccp.evaluators.verdict import Verdict
from ccp.knowledge.graph import TestSpec
from ccp.llm.budget import BudgetTracker, RunBudget
from ccp.llm.client import LLMClient, SystemBlock
from ccp.llm.prompts import load_prompt
from ccp.observability import steps
from ccp.storage.scoped import ScopedDB
from ccp.types import VerdictStatus

PROMPT = "second_reader_v1"
DISAGREED = "second_reader_disagreed"
CAP = 0.5  # a disputed result is never confident


class SecondRead(BaseModel):
    agrees: bool
    reason: str = Field(max_length=500)


def _values(fields: dict[str, Any] | None) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in (fields or {}).items():
        out[key] = value.get("value") if isinstance(value, dict) and "value" in value else value
    return out


def review(
    db: ScopedDB,
    settings: Settings,
    llm: LLMClient | None,
    spec: TestSpec,
    verdict: Verdict,
) -> Verdict:
    if verdict.tier == 1 or verdict.status is not VerdictStatus.passed or llm is None:
        return verdict
    quotes = [c for c in verdict.citations if c.verified]
    prompt = load_prompt(PROMPT)
    user = json.dumps(
        {
            "test": f"{spec.test_id}: {spec.title}",
            "subject": verdict.subject,
            "fields": _values(verdict.extracted_fields),
            "citations": [{"supports": c.supports, "chunk_id": c.chunk_id} for c in quotes],
        },
        sort_keys=True,
    )
    tracker = BudgetTracker(RunBudget.from_settings(settings))
    with steps.step(
        "evaluate.second_reader",
        layer="evaluators",
        kind="generation",
        tenant=db.tenant,
        inputs={"verdict_id": verdict.verdict_id, "test_id": spec.test_id},
    ) as s:
        s.model(
            settings.ccp_second_reader_model, prompt_version=PROMPT, prompt_sha256=prompt.sha256
        )
        read = llm.extract(
            schema=SecondRead,
            system=[SystemBlock(prompt.text, cache=True)],
            user=user,
            model=settings.ccp_second_reader_model,
            tracker=tracker,
            max_tokens=512,
            documents=[f"[{c.chunk_id}] {c.quote}" for c in quotes],
        )
        answer: SecondRead = read.parsed
        budget_gate.record_spend(
            db, read.usage.usd, read.usage.input_tokens + read.usage.output_tokens, s.step_id
        )
        s.set_output({"agrees": answer.agrees, "reason": answer.reason})
    kept = {
        **(verdict.response_json or {}),
        "second_read": {
            "model": read.model,
            "prompt_sha256": prompt.sha256,
            "agrees": answer.agrees,
            "reason": answer.reason,
        },
    }
    if answer.agrees:
        return verdict.model_copy(update={"response_json": kept})
    return verdict.model_copy(
        update={
            "status": VerdictStatus.needs_review,
            "reasoning": f"{verdict.reasoning} | a second reader did not confirm: {answer.reason}",
            "guardrail_events": (*verdict.guardrail_events, DISAGREED),
            "confidence": min(verdict.confidence, CAP),
            "confidence_cap": min(verdict.confidence_cap, CAP),
            "response_json": kept,
        }
    )
