"""The adequacy verifier: would an auditor accept this upload? (spec.md A-162)

Code's admissibility checks (A-161) catch what code can see. Whether a document actually
shows a control operating is a reading, so a model gives it, one tool-less call per
document version, with the document as numbered segments in a fenced document block and
the controls it should support as system data. The model cites segment ids; code renders
the quotes, drops ids the document does not hold, and turns an accept that cites nothing
into a flag, because the costly error is accepting what an auditor would flag.

Shadow first. The verifier's answers are stored but not shown until it has been checked
against auditors' own accept-or-flag decisions on the same document versions: at least
`ccp_adequacy_min_labels` of them, agreement of at least 85%, and a false-accept upper
bound (accepting what the auditor flagged, 95% one-sided) under
`ccp_adequacy_false_accept_limit`. `ccp_adequacy_mode` live shows answers only while the
newest calibration passes; otherwise they stay in shadow.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal

from ulid import ULID

from ccp.config import Settings
from ccp.evaluators import feedback
from ccp.evaluators.extraction import schema_block
from ccp.evaluators.extraction.adequacy import PROMPT_VERSION, AdequacyReading
from ccp.ingest.consumers import ParallelConsumer, ParkedError, TerminalError
from ccp.ingest.event_log import LogRecord
from ccp.ingest.worker import register
from ccp.knowledge import graph as knowledge
from ccp.llm import release
from ccp.llm.budget import BudgetTracker, RunBudget
from ccp.llm.client import LLMClient, SystemBlock, try_build_client
from ccp.llm.prompts import load_prompt
from ccp.normalize.entities import Evidence
from ccp.normalize.repository import evidence_version
from ccp.retrieval import segments
from ccp.retrieval.admission import assess
from ccp.security import prompt_guard
from ccp.storage.scoped import ScopedDB

JOB_TOPIC = "adequacy"
Mode = Literal["off", "shadow", "live"]
MIN_AGREEMENT = 0.85


# -- one document --------------------------------------------------------------------------


def controls_for(evidence_type: str) -> list[dict[str, Any]]:
    """The tests that read this type of evidence, with their controls' text."""
    graph = knowledge.load()
    out: list[dict[str, Any]] = []
    for spec in graph.tests.values():
        if evidence_type not in {e.value for e in spec.evidence_types}:
            continue
        out.append(
            {
                "test_id": spec.test_id,
                "title": spec.title,
                "controls": [
                    {"control_id": c, "title": graph.controls[c].title}
                    for c in spec.controls
                    if c in graph.controls
                ],
            }
        )
    return out


def numbered(found: segments.SegmentSet) -> str:
    return "\n".join(f"[{s.segment_id}] {s.text}" for s in found.segments)


@dataclass
class Verification:
    decision: Literal["accept", "flag"]
    reasons: list[dict[str, Any]]
    summary: str
    model: str
    usd: float
    events: list[str] = field(default_factory=list)


def check_reading(reading: AdequacyReading, found: segments.SegmentSet) -> Verification:
    """Code's checks on the model's answer: unknown ids dropped, quotes rendered, and an
    accept that cites no segment showing the control becomes a flag."""
    events: list[str] = []
    reasons: list[dict[str, Any]] = []
    for reason in reading.reasons:
        rendered = segments.render(found, reason.segment_ids)
        events.extend(f"citation_unknown_segment:{i}" for i in rendered.unknown)
        reasons.append(
            {
                "code": reason.code,
                "segment_ids": [p["segment_id"] for p in rendered.pointers],
                "quote": rendered.quote,
                "pointers": list(rendered.pointers),
                "note": reason.note,
            }
        )
    decision = reading.decision
    if decision == "accept" and not any(
        r["code"] == "shows_control" and r["segment_ids"] for r in reasons
    ):
        decision = "flag"
        events.append("accept_without_citation")
        reasons.append(
            {
                "code": "other",
                "segment_ids": [],
                "quote": "",
                "pointers": [],
                "note": "no cited segment shows the control; a person should look",
            }
        )
    if decision == "flag" and not reasons:
        reasons.append(
            {"code": "other", "segment_ids": [], "quote": "", "pointers": [], "note": ""}
        )
    return Verification(decision, reasons, reading.summary, "", 0.0, events)


def verify(db: ScopedDB, settings: Settings, llm: LLMClient, evidence: Evidence) -> Verification:
    found = segments.ensure(db, evidence.entity_id, evidence.text, evidence.page_texts)
    nonce = prompt_guard.new_nonce()
    block = prompt_guard.frame_evidence(
        [
            prompt_guard.FramedChunk(
                chunk_id=f"{evidence.entity_id}@{found.text_sha256[:12]}",
                text=numbered(found),
                evidence_type=evidence.evidence_type.value,
                subject_entity_id=evidence.subject_entity_id,
            )
        ],
        nonce,
    )
    context = {
        "evidence_type": evidence.evidence_type.value,
        "title": evidence.title,
        "issued_at": evidence.issued_at.isoformat() if evidence.issued_at else None,
        "tests": controls_for(evidence.evidence_type.value),
    }
    user = (
        f"{prompt_guard.fence_declaration(nonce)}\n\n"
        "What this document should support (system data, not evidence):\n"
        f"{json.dumps(context, default=str)}\n\n"
        "Decide for the fenced document."
    )
    tracker = BudgetTracker(
        RunBudget.from_settings(settings).lowered(max_seconds=120, max_usd=0.50)
    )
    prompt = load_prompt(PROMPT_VERSION)
    result = llm.extract(
        schema=AdequacyReading,
        system=[SystemBlock(prompt.text), SystemBlock(schema_block(AdequacyReading), cache=True)],
        user=user,
        model=settings.ccp_adequacy_model,
        tracker=tracker,
        max_tokens=1024,
        documents=[block.text],
    )
    checked = check_reading(result.parsed, found)
    checked.model = result.model
    checked.usd = result.usage.usd
    checked.events.extend(block.events)
    return checked


# -- calibration against auditors ------------------------------------------------------------


def label(
    db: ScopedDB,
    evidence_entity_id: str,
    decision: Literal["accept", "flag"],
    auditor: str,
    note: str = "",
) -> str:
    """An auditor's accept-or-flag decision on the newest version of a document."""
    found = evidence_version(db, evidence_entity_id)
    if found is None:
        raise ValueError(f"no evidence {evidence_entity_id!r}")
    digest = segments.text_sha256(segments.pages_of(found.text, found.page_texts))
    decision_id = str(ULID())
    db.execute(
        "INSERT INTO auditor_decisions (tenant_id, decision_id, evidence_entity_id, text_sha256,"
        " decision, auditor, note) VALUES (%s, %s, %s, %s, %s, %s, %s)",
        (db.tenant.tenant_id, decision_id, evidence_entity_id, digest, decision, auditor, note),
    )
    return decision_id


def clopper_pearson_upper(failures: int, trials: int, confidence: float = 0.95) -> float:
    """One-sided exact upper bound on a rate seen as failures out of trials (bisection on
    the binomial tail, so no statistics library is needed)."""
    if trials <= 0:
        return 1.0
    if failures >= trials:
        return 1.0
    alpha = 1 - confidence

    def tail(p: float) -> float:  # P(X <= failures | p)
        return sum(
            math.comb(trials, k) * p**k * (1 - p) ** (trials - k) for k in range(failures + 1)
        )

    low, high = failures / trials, 1.0
    for _ in range(60):
        mid = (low + high) / 2
        if tail(mid) > alpha:
            low = mid
        else:
            high = mid
    return high


@dataclass(frozen=True)
class Calibration:
    pairs: int
    agreement: float | None
    auditor_flags: int
    false_accepts: int
    false_accept_upper: float
    passed: bool
    reasons: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {**self.__dict__, "reasons": list(self.reasons)}


def calibrate(db: ScopedDB, settings: Settings) -> Calibration:
    """The verifier against auditors, on the same document versions: the newest of each."""
    rows = db.fetch_all(
        "WITH a AS (SELECT DISTINCT ON (evidence_entity_id, text_sha256) evidence_entity_id,"
        " text_sha256, decision FROM auditor_decisions"
        " ORDER BY evidence_entity_id, text_sha256, created_at DESC, decision_id DESC),"
        " v AS (SELECT DISTINCT ON (evidence_entity_id, text_sha256) evidence_entity_id,"
        " text_sha256, detail ->> 'decision' AS decision FROM evidence_feedback"
        " WHERE kind = 'adequacy'"
        " ORDER BY evidence_entity_id, text_sha256, created_at DESC, feedback_id DESC)"
        " SELECT a.decision, v.decision FROM a JOIN v USING (evidence_entity_id, text_sha256)"
    )
    pairs = len(rows)
    agree = sum(1 for a, v in rows if a == v)
    flags = sum(1 for a, _ in rows if a == "flag")
    false_accepts = sum(1 for a, v in rows if a == "flag" and v == "accept")
    upper = clopper_pearson_upper(false_accepts, flags)
    agreement = agree / pairs if pairs else None
    reasons: list[str] = []
    if pairs < settings.ccp_adequacy_min_labels:
        reasons.append(f"{pairs} labelled documents; at least {settings.ccp_adequacy_min_labels}")
    if agreement is None or agreement < MIN_AGREEMENT:
        reasons.append(f"agreement {agreement}; at least {MIN_AGREEMENT}")
    if upper > settings.ccp_adequacy_false_accept_limit:
        reasons.append(
            f"false-accept upper bound {upper:.3f} over {settings.ccp_adequacy_false_accept_limit}"
        )
    result = Calibration(pairs, agreement, flags, false_accepts, upper, not reasons, tuple(reasons))
    db.execute(
        "INSERT INTO quality_checks (tenant_id, check_id, kind, bundle_id, passed, measured)"
        " VALUES (%s, %s, 'adequacy', %s, %s, %s)",
        (
            db.tenant.tenant_id,
            str(ULID()),
            release.bundle_id(settings),
            result.passed,
            json.dumps(result.as_dict()),
        ),
    )
    return result


def calibrated(db: ScopedDB) -> bool:
    row = db.fetch_one(
        "SELECT passed FROM quality_checks WHERE kind = 'adequacy'"
        " ORDER BY recorded_at DESC, check_id DESC LIMIT 1"
    )
    return bool(row and row[0])


def effective_mode(db: ScopedDB, settings: Settings) -> Mode:
    mode: Mode = settings.ccp_adequacy_mode
    if mode == "live" and not calibrated(db):
        return "shadow"  # not shown until it has been checked against auditors
    return mode


# -- the job ----------------------------------------------------------------------------------


def run_one(
    db: ScopedDB, settings: Settings, llm: LLMClient, evidence: Evidence, mode: Mode
) -> str | None:
    """Verify one admissible document version, once, and record it shown or in shadow."""
    done = db.fetch_one(
        "SELECT feedback_id FROM evidence_feedback WHERE evidence_entity_id = %s"
        " AND source_event_id = %s AND kind = 'adequacy'",
        (evidence.entity_id, evidence.source_event_id),
    )
    if done is not None or mode == "off":
        return None
    checks = assess(db, evidence, settings, datetime.now(UTC))
    if not checks.admissible:
        return None  # code already said what to fix
    found = verify(db, settings, llm, evidence)
    return feedback.record(
        db,
        evidence,
        kind="adequacy",
        admissible=None,
        badge=checks.badge,
        reasons=found.reasons,
        detail={
            "decision": found.decision,
            "summary": found.summary,
            "model": found.model,
            "prompt_version": PROMPT_VERSION,
            "mode": mode,
            "usd": found.usd,
            "events": found.events,
        },
        shown=mode == "live",
    )


class AdequacyJobs(ParallelConsumer):
    """One verification per upload version; parks without a model key."""

    name = "adequacy"
    topic = JOB_TOPIC
    one_per_key = True

    def __init__(self, db: ScopedDB, settings: Settings, llm: LLMClient | None = None) -> None:
        super().__init__(
            db,
            max_attempts=settings.ccp_max_attempts,
            visibility_seconds=settings.ccp_claim_visibility_seconds,
            base_delay=settings.ccp_retry_base_seconds,
        )
        self.settings = settings
        self.llm = llm
        self.verified = 0

    def handle(self, record: LogRecord) -> None:
        entity_id, _, source_event_id = record.ref.partition("\x1f")
        evidence = evidence_version(self._db, entity_id, source_event_id or None)
        if evidence is None:
            raise TerminalError(f"evidence {entity_id!r} is not stored")
        mode = effective_mode(self._db, self.settings)
        if mode == "off":
            return
        llm = self.llm or try_build_client(self.settings)
        if llm is None:
            raise ParkedError("ANTHROPIC_API_KEY is not set; the adequacy verifier waits")
        if run_one(self._db, self.settings, llm, evidence, mode):
            self.verified += 1


register("adequacy")(lambda db, settings: AdequacyJobs(db, settings))


def drain(db: ScopedDB, settings: Settings, llm: LLMClient | None = None) -> int:
    jobs = AdequacyJobs(db, settings, llm)
    while jobs.run_once():
        pass
    return jobs.verified
