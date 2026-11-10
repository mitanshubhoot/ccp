"""The run log: every model turn and tool call of an agent run, in Postgres (spec.md A-109).

    node ── request digest ──► agent_steps has (thread, model, digest)?
                                  yes ─► the recorded turn, no model call (a resume, a replay)
                                  no  ─► replaying? ─► ReplayDiverged: this request is new
                                         else call the model, record the turn, return it
    tool call ── tool_use id ──► the recorded result, or run the tool and record it

The record is written as each call returns, before the run moves on, so a worker that dies
mid-run leaves every completed call behind it. LangGraph re-runs an interrupted node from its
start, and the node asks the log first: each turn it already took comes back as recorded, its
tools return what they returned, so the requests come out the same and nothing is sampled
twice. The fence nonce a node uses is logged with its first call for the same reason.
A replay re-runs a recorded run against its log alone and refuses any request the log does
not hold: an empty refusal list is the proof that the run is reproducible.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from ulid import ULID

from ccp.llm.client import ChatTurn, Extraction, ToolCall, Usage
from ccp.security import prompt_guard
from ccp.storage.scoped import ScopedDB


class ReplayDiverged(RuntimeError):
    """A replayed run made a request its log does not hold."""


def _usage(u: dict[str, Any]) -> Usage:
    return Usage(**{k: u[k] for k in Usage.__dataclass_fields__ if k in u})


class RunLog:
    def __init__(
        self, db: ScopedDB, thread_id: str, *, source: str | None = None, replay: bool = False
    ) -> None:
        self._db = db
        self.thread_id = thread_id
        self.source = source or thread_id  # a replay reads the original run's log
        self.replay = replay
        self.served = 0  # calls answered from the log
        self.recorded = 0  # calls made and written

    # -- storage ---------------------------------------------------------------------------

    def _find(self, kind: str, key: str) -> dict[str, Any] | None:
        row = self._db.fetch_one(
            "SELECT response FROM agent_steps WHERE thread_id = %s AND kind = %s AND call_key = %s",
            (self.source, kind, key),
        )
        if row is None:
            return None
        value = row[0]
        return json.loads(value) if isinstance(value, str) else dict(value)

    def _write(
        self,
        node: str,
        kind: str,
        key: str,
        request: dict[str, Any],
        response: dict[str, Any],
        model: str | None = None,
        usage: dict[str, Any] | None = None,
    ) -> None:
        with self._db.transaction() as cur:
            cur.execute(
                "INSERT INTO agent_steps (step_id, tenant_id, thread_id, seq, node, kind, call_key,"
                " request, response, model, usage)"
                " SELECT %s, %s, %s, coalesce(max(seq), 0) + 1, %s, %s, %s, %s, %s, %s, %s"
                " FROM agent_steps WHERE thread_id = %s"
                " ON CONFLICT (tenant_id, thread_id, kind, call_key) DO NOTHING",
                (
                    str(ULID()),
                    self._db.tenant.tenant_id,
                    self.thread_id,
                    node,
                    kind,
                    key,
                    json.dumps(request, default=str),
                    json.dumps(response, default=str),
                    model,
                    json.dumps(usage) if usage is not None else None,
                    self.thread_id,
                ),
            )
        self.recorded += 1

    def _served(self, found: dict[str, Any]) -> dict[str, Any]:
        self.served += 1
        return found

    # -- what nodes call -------------------------------------------------------------------

    def nonce(self, node: str) -> str:
        """The fence nonce this node used the first time, so a resumed request is the same."""
        key = f"nonce:{node}"
        found = self._find("nonce", key)
        if found is not None:
            return str(found["nonce"])
        if self.replay:
            raise ReplayDiverged(f"{node}: no fence nonce was recorded")
        nonce = prompt_guard.new_nonce()
        self._write(node, "nonce", key, {}, {"nonce": nonce})
        return nonce

    def chat(self, node: str, digest: str, call: Callable[[], ChatTurn]) -> tuple[ChatTurn, bool]:
        """The turn for this request: recorded (True) or sampled now and recorded (False)."""
        found = self._find("model", digest)
        if found is not None:
            self._served(found)
            return (
                ChatTurn(
                    text=str(found["text"]),
                    tool_calls=tuple(ToolCall(**c) for c in found["tool_calls"]),
                    stop_reason=str(found["stop_reason"]),
                    usage=_usage(found["usage"]),
                    model=str(found["model"]),
                    request=dict(found["request"]),
                    response=dict(found["response"]),
                    assistant_content=list(found["assistant_content"]),
                ),
                True,
            )
        if self.replay:
            raise ReplayDiverged(f"{node}: request {digest[:12]} is not in the recorded run")
        turn = call()
        body = {
            "text": turn.text,
            "tool_calls": [c.__dict__ for c in turn.tool_calls],
            "stop_reason": turn.stop_reason,
            "usage": turn.usage.__dict__,
            "model": turn.model,
            "request": turn.request,
            "response": turn.response,
            "assistant_content": turn.assistant_content,
        }
        self._write(node, "model", digest, turn.request, body, turn.model, turn.usage.__dict__)
        return turn, False

    def extract(
        self, node: str, digest: str, schema: type[Any], call: Callable[[], Extraction]
    ) -> tuple[Extraction, bool]:
        found = self._find("model", digest)
        if found is not None:
            self._served(found)
            return (
                Extraction(
                    parsed=schema.model_validate(found["parsed"]),
                    usage=_usage(found["usage"]),
                    model=str(found["model"]),
                    request=dict(found["request"]),
                    response=dict(found["response"]),
                ),
                True,
            )
        if self.replay:
            raise ReplayDiverged(f"{node}: request {digest[:12]} is not in the recorded run")
        extraction = call()
        body = {
            "parsed": extraction.parsed.model_dump(mode="json"),
            "usage": extraction.usage.__dict__,
            "model": extraction.model,
            "request": extraction.request,
            "response": extraction.response,
        }
        self._write(
            node,
            "model",
            digest,
            extraction.request,
            body,
            extraction.model,
            extraction.usage.__dict__,
        )
        return extraction, False

    def tool(
        self,
        node: str,
        call_id: str,
        name: str,
        arguments: dict[str, Any],
        run: Callable[[], dict[str, Any]],
    ) -> dict[str, Any]:
        """The tool's result for this call: as recorded, or run now and recorded."""
        found = self._find("tool", call_id)
        if found is not None:
            return self._served(found)
        if self.replay:
            raise ReplayDiverged(f"{node}: tool call {call_id} ({name}) is not in the recorded run")
        result = run()
        self._write(node, "tool", call_id, {"name": name, "arguments": arguments}, result)
        return result
