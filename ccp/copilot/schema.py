"""CopilotAnswer records (spec.md Layer 17) and their persistence: one row per turn."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field
from ulid import ULID

from ccp.evaluators.verdict import Citation
from ccp.storage.scoped import ScopedDB
from ccp.tenancy import TenantId


class CopilotAnswer(BaseModel):
    model_config = ConfigDict(frozen=True)

    answer_id: str = Field(default_factory=lambda: str(ULID()))
    tenant_id: TenantId
    thread_id: str
    turn: int
    question: str
    intent: str
    text: str
    citations: tuple[Citation, ...] = ()  # verified only
    requested_actions: tuple[dict[str, Any], ...] = ()  # bounded and capped by the graph
    model: str | None
    prompt_version: str
    guardrail_events: tuple[str, ...] = ()
    latency_ms: float | None = None
    first_token_ms: float | None = None  # streaming arrives with Layer 12's Phase 16 work
    fence_nonce: str | None = None
    request_json: dict[str, Any] | None = None
    response_json: dict[str, Any] | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


_COLUMNS = (
    "answer_id, tenant_id, thread_id, turn, question, intent, text, citations,"
    " requested_actions, model, prompt_version, guardrail_events, latency_ms, first_token_ms,"
    " fence_nonce, request_json, response_json, created_at"
)


def persist(db: ScopedDB, answer: CopilotAnswer) -> None:
    db.execute(
        f"INSERT INTO copilot_answers ({_COLUMNS})"
        " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
        (
            answer.answer_id,
            answer.tenant_id,
            answer.thread_id,
            answer.turn,
            answer.question,
            answer.intent,
            answer.text,
            json.dumps([c.model_dump(mode="json") for c in answer.citations]),
            json.dumps(list(answer.requested_actions), default=str),
            answer.model,
            answer.prompt_version,
            json.dumps(list(answer.guardrail_events)),
            answer.latency_ms,
            answer.first_token_ms,
            answer.fence_nonce,
            json.dumps(answer.request_json, default=str) if answer.request_json else None,
            json.dumps(answer.response_json, default=str) if answer.response_json else None,
            answer.created_at,
        ),
    )


def _load(value: Any) -> Any:
    return json.loads(value) if isinstance(value, str) else value


def _from_row(r: tuple[Any, ...]) -> CopilotAnswer:
    return CopilotAnswer(
        answer_id=str(r[0]),
        tenant_id=TenantId(str(r[1])),
        thread_id=str(r[2]),
        turn=int(r[3]),
        question=str(r[4]),
        intent=str(r[5]),
        text=str(r[6]),
        citations=tuple(Citation.model_validate(c) for c in _load(r[7])),
        requested_actions=tuple(_load(r[8])),
        model=r[9],
        prompt_version=str(r[10]),
        guardrail_events=tuple(_load(r[11])),
        latency_ms=r[12],
        first_token_ms=r[13],
        fence_nonce=r[14],
        request_json=_load(r[15]) if r[15] is not None else None,
        response_json=_load(r[16]) if r[16] is not None else None,
        created_at=r[17],
    )


def get(db: ScopedDB, answer_id: str) -> CopilotAnswer | None:
    row = db.fetch_one(f"SELECT {_COLUMNS} FROM copilot_answers WHERE answer_id = %s", (answer_id,))
    return _from_row(row) if row else None


def for_thread(db: ScopedDB, thread_id: str) -> list[CopilotAnswer]:
    rows = db.fetch_all(
        f"SELECT {_COLUMNS} FROM copilot_answers WHERE thread_id = %s ORDER BY turn", (thread_id,)
    )
    return [_from_row(r) for r in rows]


def list_recent(db: ScopedDB, limit: int = 50) -> list[CopilotAnswer]:
    rows = db.fetch_all(
        f"SELECT {_COLUMNS} FROM copilot_answers ORDER BY created_at DESC LIMIT %s", (limit,)
    )
    return [_from_row(r) for r in rows]
