"""Judge calibration against human labels (spec.md Layer 11 "Calibration").

``evals/labeled/*.yaml`` holds the labeled set: a fixture-shaped output, its rubric, and a
human label per criterion plus overall. ``calibrate`` runs the judge over every case and
reports agreement and Cohen's kappa per criterion and overall, each with a bootstrap
95 percent interval (wide at n = 20, and the header says so), rubric-paraphrase
robustness, the order and length bias checks, a confusion matrix per criterion, and,
where a second annotator labelled an overlap, the human-human kappa as the ceiling.
"""

from __future__ import annotations

import json
import random
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field

from ccp.config import REPO_ROOT
from ccp.evals import judge as judging
from ccp.evals.judge import Rubric
from ccp.llm.budget import BudgetTracker
from ccp.llm.client import LLMClient
from ccp.llm.models import family

LABELED_DIR = REPO_ROOT / "evals" / "labeled"


class LabeledCase(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    kind: Literal["verdict", "copilot"]
    category: str  # clean_pass | clean_fail | borderline | copilot_ok | copilot_confidently_wrong
    rubric: str
    input: dict[str, Any]
    output: dict[str, Any]
    labels: dict[str, bool]  # c1..cn
    overall: bool
    annotator: str = "author"
    second_labels: dict[str, bool] | None = None
    second_overall: bool | None = None
    note: str | None = None
    origin: Literal["fixture", "customer"] = "fixture"  # customer: a reviewer's label (A-103)


def load_labeled(labeled_dir: Path = LABELED_DIR) -> list[LabeledCase]:
    cases: list[LabeledCase] = []
    for path in sorted(labeled_dir.glob("*.yaml")):
        loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or []
        cases.extend(LabeledCase.model_validate(item) for item in loaded)
    ids = [c.id for c in cases]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate labeled case ids")
    return cases


def agreement_rate(a: Sequence[bool], b: Sequence[bool]) -> float:
    pairs = list(zip(a, b, strict=True))
    return sum(x == y for x, y in pairs) / len(pairs) if pairs else 1.0


def cohen_kappa(a: Sequence[bool], b: Sequence[bool]) -> float:
    pairs = list(zip(a, b, strict=True))
    n = len(pairs)
    if n == 0:
        return 1.0
    po = sum(x == y for x, y in pairs) / n
    pa = sum(x for x, _ in pairs) / n
    pb = sum(y for _, y in pairs) / n
    pe = pa * pb + (1 - pa) * (1 - pb)
    if pe >= 1.0:
        return 1.0 if po >= 1.0 else 0.0
    return (po - pe) / (1 - pe)


def bootstrap_ci(
    a: Sequence[bool],
    b: Sequence[bool],
    stat: Callable[[Sequence[bool], Sequence[bool]], float],
    *,
    n_boot: int = 1000,
    seed: int = 7,
) -> tuple[float, float]:
    """Percentile 95 percent interval of ``stat`` over resampled (human, judge) pairs."""
    pairs = list(zip(a, b, strict=True))
    if not pairs:
        return (0.0, 0.0)
    rng = random.Random(seed)
    values: list[float] = []
    for _ in range(n_boot):
        sample = [pairs[rng.randrange(len(pairs))] for _ in pairs]
        values.append(stat([x for x, _ in sample], [y for _, y in sample]))
    values.sort()
    lo = values[int(0.025 * (n_boot - 1))]
    hi = values[int(0.975 * (n_boot - 1))]
    return (lo, hi)


class CriterionStats(BaseModel):
    n: int
    agreement: float
    kappa: float
    ci_low: float
    ci_high: float
    human_true: int
    judge_true: int


class CalibrationReport(BaseModel):
    created_at: datetime
    judge_model: str | None
    n: int
    header: str
    per_criterion: dict[str, CriterionStats]  # "<rubric>:c1"
    overall: CriterionStats
    paraphrase: dict[str, float]  # judge-vs-judge agreement per criterion key, and "overall"
    bias: dict[str, float]  # order_agreement, length_agreement, *_overall_agrees rates
    confusion: dict[str, list[list[int]]]  # [[tn, fp], [fn, tp]] with human as truth
    human_human_kappa: float | None
    cases: list[dict[str, Any]] = Field(default_factory=list)


def _stats(human: Sequence[bool], judged: Sequence[bool], n_boot: int, seed: int) -> CriterionStats:
    lo, hi = bootstrap_ci(human, judged, cohen_kappa, n_boot=n_boot, seed=seed)
    return CriterionStats(
        n=len(human),
        agreement=agreement_rate(human, judged),
        kappa=cohen_kappa(human, judged),
        ci_low=lo,
        ci_high=hi,
        human_true=sum(human),
        judge_true=sum(judged),
    )


def _confusion(human: Sequence[bool], judged: Sequence[bool]) -> list[list[int]]:
    tn = sum(1 for h, j in zip(human, judged, strict=True) if not h and not j)
    fp = sum(1 for h, j in zip(human, judged, strict=True) if not h and j)
    fn = sum(1 for h, j in zip(human, judged, strict=True) if h and not j)
    tp = sum(1 for h, j in zip(human, judged, strict=True) if h and j)
    return [[tn, fp], [fn, tp]]


def calibrate(
    llm: LLMClient,
    cases: list[LabeledCase],
    *,
    model: str,
    tracker: BudgetTracker,
    rubrics_dir: Path = judging.RUBRICS_DIR,
    n_boot: int = 1000,
    seed: int = 7,
    bias_on: int | None = None,
) -> CalibrationReport:
    """Run the judge over the labeled set (normal and paraphrased wording, plus the bias
    variants on ``bias_on`` cases; every case by default) and compare with the labels.
    ``tracker`` is the budget template: every judge call gets a fresh tracker with the same
    budget, because one run budget cannot cover the eighty-odd calls a calibration makes
    (its wall clock and token caps are sized for one verdict)."""

    def fresh() -> BudgetTracker:
        return BudgetTracker(tracker.budget, clock=tracker.clock)

    customer = [c.id for c in cases if c.origin == "customer"]
    if customer and family(model) != "anthropic":
        raise judging.DataBoundary(
            f"judge model {model!r} is not a Claude model; reviewer labels {customer[:3]}"
            " come from customer data"
        )
    rubrics: dict[str, Rubric] = {}
    per_key_human: dict[str, list[bool]] = {}
    per_key_judge: dict[str, list[bool]] = {}
    per_key_para: dict[str, list[bool]] = {}
    overall_human: list[bool] = []
    overall_judge: list[bool] = []
    overall_para: list[bool] = []
    bias_rows: list[dict[str, float | bool]] = []
    rows: list[dict[str, Any]] = []
    second_pairs: list[tuple[bool, bool]] = []
    judge_model: str | None = None
    for index, case in enumerate(cases):
        rubric = rubrics.get(case.rubric)
        if rubric is None:
            rubric = judging.load_rubric(case.rubric, rubrics_dir)
            rubrics[case.rubric] = rubric
        plain = judging.judge(
            llm, rubric, case_input=case.input, output=case.output, model=model, tracker=fresh()
        )
        para = judging.judge(
            llm,
            rubric,
            case_input=case.input,
            output=case.output,
            model=model,
            tracker=fresh(),
            variant="paraphrase",
        )
        judge_model = judge_model or plain.model
        for criterion, para_criterion in zip(plain.criteria, para.criteria, strict=True):
            key = f"{rubric.name}:{criterion.name}"
            label = case.labels.get(criterion.name)
            if label is None:
                raise ValueError(f"{case.id}: no human label for {criterion.name}")
            per_key_human.setdefault(key, []).append(label)
            per_key_judge.setdefault(key, []).append(criterion.met)
            per_key_para.setdefault(key, []).append(para_criterion.met)
        overall_human.append(case.overall)
        overall_judge.append(plain.overall_pass)
        overall_para.append(para.overall_pass)
        if bias_on is None or index < bias_on:
            bias_rows.append(
                judging.bias_checks(
                    llm,
                    rubric,
                    case_input=case.input,
                    output=case.output,
                    model=model,
                    tracker=fresh(),
                    base=plain,
                )
            )
        if case.second_overall is not None:
            second_pairs.append((case.overall, case.second_overall))
        rows.append(
            {
                "id": case.id,
                "category": case.category,
                "human_overall": case.overall,
                "judge_overall": plain.overall_pass,
                "criteria": {
                    c.name: {"human": case.labels[c.name], "judge": c.met} for c in plain.criteria
                },
            }
        )
    per_criterion = {
        key: _stats(per_key_human[key], per_key_judge[key], n_boot, seed)
        for key in sorted(per_key_human)
    }
    paraphrase = {
        key: agreement_rate(per_key_judge[key], per_key_para[key]) for key in sorted(per_key_judge)
    }
    paraphrase["overall"] = agreement_rate(overall_judge, overall_para)
    bias: dict[str, float] = {}
    if bias_rows:
        for name in (
            "order_agreement",
            "length_agreement",
            "order_overall_agrees",
            "length_overall_agrees",
        ):
            bias[name] = sum(float(r[name]) for r in bias_rows) / len(bias_rows)
    confusion = {
        key: _confusion(per_key_human[key], per_key_judge[key]) for key in sorted(per_key_human)
    }
    confusion["overall"] = _confusion(overall_human, overall_judge)
    n = len(cases)
    header = (
        f"Judge calibration over n = {n} labeled cases. At this size every interval below is"
        " wide; the number shows the mechanism and catches a judge that is systematically"
        " lenient on the injection criterion, it is not a precise estimate. Intervals are"
        f" bootstrap 95 percent ({n_boot} resamples, seed {seed}); labels are single-annotator"
        " unless a second annotator column is present."
    )
    return CalibrationReport(
        created_at=datetime.now(UTC),
        judge_model=judge_model,
        n=n,
        header=header,
        per_criterion=per_criterion,
        overall=_stats(overall_human, overall_judge, n_boot, seed),
        paraphrase=paraphrase,
        bias=bias,
        confusion=confusion,
        human_human_kappa=(
            cohen_kappa([a for a, _ in second_pairs], [b for _, b in second_pairs])
            if len(second_pairs) >= 2
            else None
        ),
        cases=rows,
    )


def format_report(report: CalibrationReport) -> str:
    lines = [report.header, ""]
    header = f"{'criterion':<26} {'agree':>6} {'kappa':>7} {'95% CI':>16}"
    lines.append(header + f" {'human+':>7} {'judge+':>7}")
    for key, s in report.per_criterion.items():
        lines.append(
            f"{key:<26} {s.agreement:>6.2f} {s.kappa:>7.2f} [{s.ci_low:>6.2f}, {s.ci_high:>6.2f}]"
            f" {s.human_true:>7} {s.judge_true:>7}"
        )
    s = report.overall
    lines.append(
        f"{'overall_pass':<26} {s.agreement:>6.2f} {s.kappa:>7.2f}"
        f" [{s.ci_low:>6.2f}, {s.ci_high:>6.2f}] {s.human_true:>7} {s.judge_true:>7}"
    )
    lines.append("")
    lines.append("rubric-paraphrase robustness (judge vs judge, second wording):")
    for key, value in report.paraphrase.items():
        lines.append(f"  {key:<24} {value:.2f}")
    if report.bias:
        lines.append("bias checks (must agree with the plain run):")
        for key, value in report.bias.items():
            lines.append(f"  {key:<24} {value:.2f}")
    lines.append("confusion matrices [[tn, fp], [fn, tp]] with the human label as truth:")
    for key, matrix in report.confusion.items():
        lines.append(f"  {key:<24} {matrix}")
    if report.human_human_kappa is not None:
        lines.append(f"human-human kappa on the overlap: {report.human_human_kappa:.2f}")
    else:
        lines.append("human-human kappa: no second annotator overlap yet (TODOS.md)")
    return "\n".join(lines)


def write_report(report: CalibrationReport, runs_dir: Path) -> Path:
    runs_dir.mkdir(parents=True, exist_ok=True)
    stamp = report.created_at.strftime("%Y%m%dT%H%M%SZ")
    path = runs_dir / f"calibration-{stamp}.json"
    path.write_text(json.dumps(report.model_dump(mode="json"), indent=2), encoding="utf-8")
    return path
