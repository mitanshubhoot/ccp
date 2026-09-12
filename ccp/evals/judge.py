"""The LLM-as-judge (spec.md Layer 11): rubric + case input + candidate output -> one criterion
verdict per rubric line, and overall_pass. The judge is shown the rubric only, never the
expected answer; the candidate is fenced as data with the same framing as tier 2, so an
output that addresses the grader is scanned like any other evidence.

Variants exist for the bias checks (A-81): the criteria in reversed order, and a
length-matched candidate padded with content-free sentences; and for rubric-paraphrase
robustness: a second wording of every criterion from ``<rubric>.paraphrase.md``.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from ccp.config import REPO_ROOT
from ccp.evals.schema import CriterionResult, JudgeResult
from ccp.llm.budget import BudgetTracker
from ccp.llm.client import LLMClient, SystemBlock
from ccp.llm.prompts import load_prompt
from ccp.security import prompt_guard
from ccp.security.prompt_guard import FramedChunk

RUBRICS_DIR = REPO_ROOT / "evals" / "rubrics"
PROMPT_VERSION = "judge_v1"
PADDING = "This sentence is neutral padding that adds length and carries no content. "
Variant = Literal["normal", "paraphrase", "reversed", "padded"]
_CRITERION = re.compile(r"^\s*(\d+)\.\s+(.*\S)\s*$")


class Rubric(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str
    criteria: tuple[str, ...]
    paraphrases: tuple[str, ...] = ()  # same count, second wording

    def wording(self, variant: Variant) -> tuple[str, ...]:
        if variant == "paraphrase" and self.paraphrases:
            return self.paraphrases
        return self.criteria


def parse_criteria(text: str) -> tuple[str, ...]:
    found: list[tuple[int, str]] = []
    for line in text.splitlines():
        match = _CRITERION.match(line)
        if match:
            found.append((int(match.group(1)), match.group(2)))
    found.sort()
    return tuple(c for _, c in found)


def load_rubric(ref: str, rubrics_dir: Path = RUBRICS_DIR) -> Rubric:
    """``verdict_reasoning`` or ``rubrics/verdict_reasoning.md`` -> the parsed rubric."""
    name = Path(ref).stem
    path = rubrics_dir / f"{name}.md"
    criteria = parse_criteria(path.read_text(encoding="utf-8"))
    if not criteria:
        raise ValueError(f"rubric {path} has no numbered criteria")
    paraphrase_path = rubrics_dir / f"{name}.paraphrase.md"
    paraphrases: tuple[str, ...] = ()
    if paraphrase_path.exists():
        paraphrases = parse_criteria(paraphrase_path.read_text(encoding="utf-8"))
        if len(paraphrases) != len(criteria):
            raise ValueError(f"{paraphrase_path} must paraphrase every criterion, one to one")
    return Rubric(name=name, criteria=criteria, paraphrases=paraphrases)


class CriterionDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")

    number: int
    met: bool
    evidence: str


class JudgeDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")

    criteria: list[CriterionDraft]
    overall_pass: bool


def render_candidate(output: Any) -> str:
    if isinstance(output, str):
        return output
    return json.dumps(output, indent=1, sort_keys=True, default=str, ensure_ascii=False)


def rubric_block(criteria: list[tuple[int, str]]) -> str:
    lines = ["Rubric criteria, numbered as they must be answered:"]
    lines.extend(f"{n}. {text}" for n, text in criteria)
    return "\n".join(lines)


def judge(
    llm: LLMClient,
    rubric: Rubric,
    *,
    case_input: dict[str, Any],
    output: Any,
    model: str,
    tracker: BudgetTracker,
    variant: Variant = "normal",
) -> JudgeResult:
    wording = list(rubric.wording(variant))
    order = list(range(len(wording)))
    if variant == "reversed":
        order.reverse()
    shown = [(n + 1, original, wording[original]) for n, original in enumerate(order)]
    text = render_candidate(output)
    if variant == "padded":
        text = text + "\n\n" + PADDING * max(1, len(text) // len(PADDING))
    nonce = prompt_guard.new_nonce()
    framed = prompt_guard.frame_evidence(
        [FramedChunk("candidate", text, "candidate_output", None, None, "eval")], nonce
    )
    prompt = load_prompt(PROMPT_VERSION)
    system = [
        SystemBlock(prompt.text, cache=True),
        SystemBlock(rubric_block([(n, c) for n, _, c in shown])),
    ]
    user = (
        f"{prompt_guard.fence_declaration(nonce)}\n\n"
        f"Case input (system data): {json.dumps(case_input, default=str)[:6000]}\n\n"
        f"{framed.text}\n"
        "Answer every criterion by its number, in order, then overall_pass."
    )
    extraction = llm.extract(
        schema=JudgeDraft, system=system, user=user, model=model, tracker=tracker
    )
    draft = extraction.parsed
    assert isinstance(draft, JudgeDraft)
    answered = {c.number: c for c in draft.criteria}
    results: list[CriterionResult] = []
    for number, original, _ in shown:
        answer = answered.get(number)
        results.append(
            CriterionResult(
                name=f"c{original + 1}",
                met=bool(answer.met) if answer else False,
                evidence=(answer.evidence if answer else "not answered")[:300],
            )
        )
    results.sort(key=lambda r: int(r.name[1:]))
    events = list(framed.events)
    if len(answered) != len(shown):
        events.append("judge_incomplete_answer")
    return JudgeResult(
        criteria=results,
        overall_pass=bool(draft.overall_pass) and all(r.met for r in results),
        model=extraction.model,
        rubric=rubric.name,
        variant=variant,
        guardrail_events=events,
        usage={
            "input_tokens": extraction.usage.input_tokens,
            "output_tokens": extraction.usage.output_tokens,
            "cache_read": extraction.usage.cache_read_input_tokens,
            "cache_write": extraction.usage.cache_creation_input_tokens,
            "usd": extraction.usage.usd,
        },
    )


def agreement(a: JudgeResult, b: JudgeResult) -> float:
    """Fraction of criteria on which two judge results agree."""
    pairs = list(zip(a.criteria, b.criteria, strict=True))
    if not pairs:
        return 1.0
    return sum(x.met == y.met for x, y in pairs) / len(pairs)


def bias_checks(
    llm: LLMClient,
    rubric: Rubric,
    *,
    case_input: dict[str, Any],
    output: Any,
    model: str,
    tracker: BudgetTracker,
    base: JudgeResult | None = None,
) -> dict[str, float | bool]:
    """Swapped criterion order and a length-matched control must agree with the plain run."""
    plain = base or judge(
        llm, rubric, case_input=case_input, output=output, model=model, tracker=tracker
    )
    reordered = judge(
        llm,
        rubric,
        case_input=case_input,
        output=output,
        model=model,
        tracker=tracker,
        variant="reversed",
    )
    padded = judge(
        llm,
        rubric,
        case_input=case_input,
        output=output,
        model=model,
        tracker=tracker,
        variant="padded",
    )
    return {
        "order_agreement": agreement(plain, reordered),
        "order_overall_agrees": plain.overall_pass == reordered.overall_pass,
        "length_agreement": agreement(plain, padded),
        "length_overall_agrees": plain.overall_pass == padded.overall_pass,
    }
