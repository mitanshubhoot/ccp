"""The shared verdict schema and its persistence (spec.md Layer 7).

Every tier returns a Verdict. The evaluation key makes retries no-ops at the database:
(tenant, test, subject, trigger_observed_at, prompt_version, model) hashed, UNIQUE.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any, Literal

import psycopg
from pydantic import BaseModel, ConfigDict, Field
from ulid import ULID

from ccp.storage.scoped import ScopedDB
from ccp.tenancy import TenantId
from ccp.types import VerdictStatus

TriggerReason = Literal[
    "impact", "full_run", "revalidate_interval", "stale_evidence", "reevaluate", "human_decision"
]


class Citation(BaseModel):
    model_config = ConfigDict(frozen=True)

    chunk_id: str
    quote: str = Field(max_length=300)
    supports: str
    verified: bool = False


class Verdict(BaseModel):
    model_config = ConfigDict(frozen=True)

    verdict_id: str = Field(default_factory=lambda: str(ULID()))
    evaluation_key: str
    tenant_id: TenantId
    test_id: str
    subject_key: str
    status: VerdictStatus
    reasoning: str
    citations: tuple[Citation, ...] = ()
    evidence_refs: tuple[str, ...] = ()
    confidence: float = Field(ge=0, le=1)
    confidence_cap: float = Field(ge=0, le=1)
    model_confidence: float | None = None
    tier: Literal[1, 2, 3]
    model: str | None = None
    prompt_version: str | None = None
    retrieved_chunk_ids: tuple[str, ...] = ()
    guardrail_events: tuple[str, ...] = ()
    extracted_fields: dict[str, Any] | None = None
    chunker_version: str | None = None
    schema_version: str | None = None
    fence_nonce: str | None = None
    trigger_observed_at: datetime
    trigger_reason: TriggerReason = "impact"
    valid_from: datetime
    request_json: dict[str, Any] | None = None
    response_json: dict[str, Any] | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    rule_version: str | None = None  # "<rule version>:<parameters hash>" (A-101)

    @property
    def subject(self) -> str:
        """The scope without the test prefix (``HR-1|person:x`` -> ``person:x``)."""
        return self.subject_key.split("|", 1)[1] if "|" in self.subject_key else self.subject_key


def evaluation_key(
    tenant_id: str,
    test_id: str,
    subject_key: str,
    trigger_observed_at: datetime,
    prompt_version: str | None,
    model: str | None,
    *,
    rule: str,
) -> str:
    """``rule`` is the rule stamp (rule version and parameters hash, A-101): a corrected
    rule or changed parameters over the same data is a new evaluation."""
    material = "|".join(
        [
            tenant_id,
            test_id,
            subject_key,
            trigger_observed_at.astimezone(UTC).isoformat(),
            prompt_version or "",
            model or "",
            rule,
        ]
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


_COLUMNS = (
    "verdict_id, evaluation_key, tenant_id, test_id, subject_key, status, reasoning, citations,"
    " evidence_refs, confidence, confidence_cap, model_confidence, tier, model, prompt_version,"
    " retrieved_chunk_ids, guardrail_events, extracted_fields, chunker_version, schema_version,"
    " fence_nonce, trigger_observed_at, trigger_reason, valid_from, request_json, response_json,"
    " created_at, rule_version"
)
_PLACEHOLDERS = ",".join(["%s"] * (_COLUMNS.count(",") + 1))


def persist(db: ScopedDB, verdict: Verdict) -> bool:
    """Insert-or-ignore on the evaluation key. Returns False when the verdict already existed."""
    with db.transaction() as cur:
        return insert(cur, verdict)


def insert(cur: psycopg.Cursor[Any], verdict: Verdict) -> bool:
    """`persist` inside the caller's transaction, so the verdict and its announcement on the
    verdicts topic commit together or not at all (A-97)."""
    row = cur.execute(
        f"INSERT INTO verdicts ({_COLUMNS}) VALUES ({_PLACEHOLDERS})"
        " ON CONFLICT (evaluation_key) DO NOTHING RETURNING verdict_id",
        (
            verdict.verdict_id,
            verdict.evaluation_key,
            verdict.tenant_id,
            verdict.test_id,
            verdict.subject_key,
            verdict.status.value,
            verdict.reasoning,
            json.dumps([c.model_dump() for c in verdict.citations]),
            json.dumps(list(verdict.evidence_refs)),
            verdict.confidence,
            verdict.confidence_cap,
            verdict.model_confidence,
            verdict.tier,
            verdict.model,
            verdict.prompt_version,
            json.dumps(list(verdict.retrieved_chunk_ids)),
            json.dumps(list(verdict.guardrail_events)),
            json.dumps(verdict.extracted_fields) if verdict.extracted_fields is not None else None,
            verdict.chunker_version,
            verdict.schema_version,
            verdict.fence_nonce,
            verdict.trigger_observed_at,
            verdict.trigger_reason,
            verdict.valid_from,
            json.dumps(verdict.request_json, default=str) if verdict.request_json else None,
            json.dumps(verdict.response_json, default=str) if verdict.response_json else None,
            verdict.created_at,
            verdict.rule_version,
        ),
    ).fetchone()
    return row is not None


def _load(value: Any) -> Any:
    return json.loads(value) if isinstance(value, str) else value


def _from_row(r: tuple[Any, ...]) -> Verdict:
    return Verdict(
        verdict_id=r[0],
        evaluation_key=r[1],
        tenant_id=TenantId(r[2]),
        test_id=r[3],
        subject_key=r[4],
        status=VerdictStatus(r[5]),
        reasoning=r[6],
        citations=tuple(Citation(**c) for c in _load(r[7])),
        evidence_refs=tuple(_load(r[8])),
        confidence=r[9],
        confidence_cap=r[10],
        model_confidence=r[11],
        tier=r[12],
        model=r[13],
        prompt_version=r[14],
        retrieved_chunk_ids=tuple(_load(r[15])),
        guardrail_events=tuple(_load(r[16])),
        extracted_fields=_load(r[17]) if r[17] is not None else None,
        chunker_version=r[18],
        schema_version=r[19],
        fence_nonce=r[20],
        trigger_observed_at=r[21],
        trigger_reason=r[22],
        valid_from=r[23],
        request_json=_load(r[24]) if r[24] is not None else None,
        response_json=_load(r[25]) if r[25] is not None else None,
        created_at=r[26],
        rule_version=r[27],
    )


def load(db: ScopedDB, verdict_id: str) -> Verdict | None:
    row = db.fetch_one(f"SELECT {_COLUMNS} FROM verdicts WHERE verdict_id = %s", (verdict_id,))
    return _from_row(row) if row else None


def load_by_key(db: ScopedDB, key: str) -> Verdict | None:
    """The stored verdict for an evaluation key, checked before any model call (A-97)."""
    row = db.fetch_one(f"SELECT {_COLUMNS} FROM verdicts WHERE evaluation_key = %s", (key,))
    return _from_row(row) if row else None


def latest_per_subject(db: ScopedDB, test_id: str | None = None) -> list[Verdict]:
    """The newest verdict for every subject key (optionally one test's)."""
    query = (
        f"SELECT DISTINCT ON (subject_key) {_COLUMNS} FROM verdicts"
        + (" WHERE test_id = %s" if test_id else "")
        + " ORDER BY subject_key, created_at DESC, verdict_id DESC"
    )
    rows = db.fetch_all(query, (test_id,) if test_id else None)
    return [_from_row(r) for r in rows]


def all_for_test(db: ScopedDB, test_id: str) -> list[Verdict]:
    rows = db.fetch_all(
        f"SELECT {_COLUMNS} FROM verdicts WHERE test_id = %s ORDER BY created_at, verdict_id",
        (test_id,),
    )
    return [_from_row(r) for r in rows]


def count(db: ScopedDB) -> int:
    row = db.fetch_one("SELECT count(*) FROM verdicts")
    return int(row[0]) if row else 0
