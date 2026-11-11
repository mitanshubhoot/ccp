"""Framework-to-control mapping proposals, scored by code (spec.md A-164).

A new framework's requirements are mapped to CCP's controls by proposal and review, never
by a model alone. For each requirement code scores every control on two signals:

    text       how much of the requirement's wording the control's own title and
               description share (cosine over content words)
    transfer   how close the requirement is to the requirements the control already maps
               to in other frameworks; a control that covers SOC 2's MFA clause is the
               likely home of another framework's MFA clause

The score is their mean. The top three are the candidates; the proposal names every
candidate within 60% of the best, and abstains when the best is under `MIN_SCORE`, because
a weak best guess is worse than none for a reviewer. A model may then pick among the
candidates (ccp/evals/mapping.py); its choice never adds a control code did not rank.

Accepting a proposal is a reviewer's act, one mapping at a time: it adds the reference to
the control in controls.yaml (promoting a draft framework into frameworks/ on its first
accepted mapping), which makes a new catalogue version (A-128), and appends the decision
to the review log with the versions before and after.
"""

from __future__ import annotations

import json
import math
import re
import shutil
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

import yaml

from ccp.knowledge import catalogue
from ccp.knowledge.graph import KNOWLEDGE_DIR, Framework, KnowledgeGraph, Requirement

MIN_SCORE = 0.12
NEAR_BEST = 0.6
TOP_K = 3
REVIEW_LOG = "mapping_reviews.jsonl"
_STOP = frozenset(
    "a an and are as at be by for from has have in is it its of on or that the their this to"
    " with within when where which who will all any each every not no more than must should"
    " shall can may per so such they them those these there other used use".split()
)
_TOKEN = re.compile(r"[a-z][a-z0-9-]+")


def tokens(text: str) -> Counter[str]:
    words = [w.rstrip("s") if len(w) > 4 else w for w in _TOKEN.findall(text.lower())]
    return Counter(w for w in words if w not in _STOP)


def cosine(a: Counter[str], b: Counter[str]) -> float:
    shared = set(a) & set(b)
    dot = sum(a[w] * b[w] for w in shared)
    norm = math.sqrt(sum(v * v for v in a.values())) * math.sqrt(sum(v * v for v in b.values()))
    return dot / norm if norm else 0.0


def requirement_text(r: Requirement) -> str:
    return " ".join([r.title, r.intent, *r.clauses])


@dataclass(frozen=True)
class Candidate:
    control_id: str
    score: float
    text: float
    transfer: float


@dataclass(frozen=True)
class Proposal:
    framework_id: str
    requirement_id: str
    requirement_title: str
    candidates: tuple[Candidate, ...]
    proposed: tuple[str, ...]
    confidence: float
    abstained: bool
    method: str = "code"
    model_reason: str = ""
    events: tuple[str, ...] = field(default=())

    def as_dict(self) -> dict[str, Any]:
        out = asdict(self)
        out["candidates"] = [asdict(c) for c in self.candidates]
        return out


def score(graph: KnowledgeGraph, requirement: Requirement, framework_id: str) -> list[Candidate]:
    """Every control scored for one requirement, best first; the requirement's own
    framework is left out of the transfer signal, so a held-out framework stays held out."""
    asked = tokens(requirement_text(requirement))
    out: list[Candidate] = []
    for control in graph.controls.values():
        own = cosine(asked, tokens(f"{control.title} {control.description}"))
        transfer = 0.0
        for ref in control.requirements:
            fw, _, rid = ref.partition(":")
            if fw == framework_id or fw not in graph.frameworks:
                continue
            try:
                other = graph.frameworks[fw].requirement(rid)
            except KeyError:
                continue
            transfer = max(transfer, cosine(asked, tokens(requirement_text(other))))
        out.append(
            Candidate(
                control.control_id,
                round((own + transfer) / 2, 4),
                round(own, 4),
                round(transfer, 4),
            )
        )
    return sorted(out, key=lambda c: (-c.score, c.control_id))


def propose(
    graph: KnowledgeGraph, framework: Framework, min_score: float = MIN_SCORE
) -> list[Proposal]:
    out: list[Proposal] = []
    for r in framework.requirements:
        ranked = score(graph, r, framework.framework_id)[:TOP_K]
        best = ranked[0].score if ranked else 0.0
        abstained = best < min_score
        proposed = (
            () if abstained else tuple(c.control_id for c in ranked if c.score >= NEAR_BEST * best)
        )
        out.append(
            Proposal(
                framework.framework_id,
                r.requirement_id,
                r.title,
                tuple(ranked),
                proposed,
                round(best, 4),
                abstained,
            )
        )
    return out


@dataclass(frozen=True)
class HeldOut:
    framework_id: str
    requirements: int
    precision_at_3: float
    recall_at_3: float
    abstained: int


def held_out(graph: KnowledgeGraph, framework_id: str, k: int = TOP_K) -> HeldOut:
    """Precision and recall at k against the mappings a framework already has, with that
    framework's own mappings left out of the scoring: the LT5 metric on a known answer."""
    framework = graph.frameworks[framework_id]
    precisions: list[float] = []
    recalls: list[float] = []
    abstained = 0
    for r in framework.requirements:
        truth = {
            c.control_id for c in graph.controls_for_requirement(framework_id, r.requirement_id)
        }
        if not truth:
            continue
        ranked = score(graph, r, framework_id)
        if not ranked or ranked[0].score < MIN_SCORE:
            abstained += 1
        top = [c.control_id for c in ranked[:k]]
        hits = len(truth & set(top))
        precisions.append(hits / k)
        recalls.append(hits / len(truth))
    n = len(recalls)
    return HeldOut(
        framework_id,
        n,
        round(sum(precisions) / n, 4) if n else 0.0,
        round(sum(recalls) / n, 4) if n else 0.0,
        abstained,
    )


# -- review: one accepted mapping at a time ------------------------------------------------


def _add_reference(controls_path: Path, control_id: str, ref: str) -> bool:
    """Add `ref` to the control's flow-style requirements list, keeping every comment and
    the order of what is there. False when it is already mapped."""
    lines = controls_path.read_text(encoding="utf-8").split("\n")
    head = next(
        (i for i, line in enumerate(lines) if line.strip() == f"- control_id: {control_id}"), None
    )
    if head is None:
        raise ValueError(f"no control {control_id!r} in {controls_path.name}")
    for i in range(head + 1, len(lines)):
        if lines[i].lstrip().startswith("- control_id:"):
            break
        found = re.match(r"^(\s*requirements:\s*\[)(.*)(\]\s*)$", lines[i])
        if found:
            refs = [x.strip() for x in found.group(2).split(",") if x.strip()]
            if ref in refs:
                return False
            lines[i] = f"{found.group(1)}{', '.join([*refs, ref])}{found.group(3)}"
            controls_path.write_text("\n".join(lines), encoding="utf-8", newline="\n")
            return True
    raise ValueError(f"{control_id} has no flow-style requirements list")


def review(
    *,
    framework_path: Path,
    requirement_id: str,
    control_id: str,
    decision: Literal["accept", "reject"],
    reviewer: str,
    note: str = "",
    knowledge_dir: Path = KNOWLEDGE_DIR,
) -> dict[str, Any]:
    """A named reviewer's decision on one mapping. An accept writes it into the catalogue
    and so makes a new catalogue version; every decision is appended to the review log."""
    if not reviewer.strip():
        raise ValueError("a mapping is reviewed by a named person")
    framework = Framework.model_validate(yaml.safe_load(framework_path.read_text(encoding="utf-8")))
    framework.requirement(requirement_id)  # KeyError for an unknown requirement
    before = catalogue.version(knowledge_dir)
    changed = False
    if decision == "accept":
        live = knowledge_dir / "frameworks" / f"{framework.framework_id}.yaml"
        if framework_path.resolve() != live.resolve():
            if live.exists() and live.read_bytes() != framework_path.read_bytes():
                raise ValueError(f"{live.name} exists and differs from the draft")
            shutil.copyfile(framework_path, live)  # the draft goes live with this mapping
        changed = _add_reference(
            knowledge_dir / "controls.yaml",
            control_id,
            f"{framework.framework_id}:{requirement_id}",
        )
        catalogue.version.cache_clear()
    after = catalogue.version(knowledge_dir)
    entry = {
        "at": datetime.now(UTC).isoformat(),
        "framework_id": framework.framework_id,
        "requirement_id": requirement_id,
        "control_id": control_id,
        "decision": decision,
        "reviewer": reviewer,
        "note": note,
        "changed": changed,
        "catalogue_before": before,
        "catalogue_after": after,
    }
    with (knowledge_dir / REVIEW_LOG).open("a", encoding="utf-8", newline="\n") as log:
        log.write(json.dumps(entry) + "\n")
    return entry
