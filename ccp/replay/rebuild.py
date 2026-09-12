"""``ccp replay <id> [--execute]`` over verdicts, investigations, gap reports, and copilot
answers (spec.md Layer 12 "Replay").

Every model-backed record stores the request as sent and the response, plus the references
that made it: the prompt version (a git-tracked file, never edited in place), the schema
name, the retrieved chunk ids (content-hash ids over immutable text), and the fence nonce.
Rebuilding takes those references, produces the prompt text, the schema block, and the
fenced evidence again, and diffs them against the stored request; the diff must be empty.
Re-execution (``--execute``) re-sends the stored request and diffs the response, which is
exact for the stub client and only approximate for a live model.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from ccp.config import Settings
from ccp.copilot import schema as copilot_schema
from ccp.evaluators import verdict as verdicts
from ccp.evaluators.context import load_context
from ccp.evaluators.extraction import schema_block, schema_model
from ccp.evaluators.tier2 import build_query
from ccp.investigation import schema as investigations
from ccp.knowledge import graph as knowledge
from ccp.llm.budget import BudgetTracker, RunBudget
from ccp.llm.client import LLMClient, SystemBlock
from ccp.llm.prompts import load_prompt
from ccp.policy import schema as policy_schema
from ccp.security import prompt_guard
from ccp.security.prompt_guard import FramedChunk
from ccp.storage.scoped import ScopedDB


@dataclass(frozen=True)
class ReplayCall:
    name: str
    model: str | None
    prompt_version: str | None
    schema: str | None
    fenced_chunks: int
    diff: tuple[str, ...]
    request: dict[str, Any] = field(repr=False)
    response: Any = field(repr=False, default=None)


@dataclass(frozen=True)
class ReplayReport:
    kind: str
    record_id: str
    tenant_id: str
    fence_nonce: str | None
    calls: tuple[ReplayCall, ...]

    @property
    def diff(self) -> list[str]:
        return [f"{c.name}: {line}" for c in self.calls for line in c.diff]

    def format(self) -> str:
        lines = [f"{self.kind} {self.record_id} (tenant {self.tenant_id})"]
        lines.append(f"  fence nonce: {self.fence_nonce or 'none recorded'}")
        for call in self.calls:
            lines.append(
                f"  call {call.name}: model={call.model} prompt={call.prompt_version}"
                f" schema={call.schema} fenced_chunks={call.fenced_chunks}"
            )
            for line in call.diff:
                lines.append(f"    - {line}")
        lines.append(
            "  request diff: " + ("empty" if not self.diff else f"{len(self.diff)} line(s)")
        )
        return "\n".join(lines)


def _system_texts(request: dict[str, Any]) -> list[tuple[str, bool]]:
    out: list[tuple[str, bool]] = []
    for block in request.get("system") or []:
        if isinstance(block, dict):
            cached = bool(block.get("cache")) or "cache_control" in block
            out.append((str(block.get("text", "")), cached))
        else:
            out.append((str(block), False))
    return out


def _user_text(request: dict[str, Any]) -> str | None:
    if isinstance(request.get("user"), str):
        return str(request["user"])
    messages = request.get("messages") or []
    if messages and isinstance(messages[0], dict):
        content = messages[0].get("content")
        if isinstance(content, str):
            return content
    return None


def _schema_name(request: dict[str, Any]) -> str | None:
    value = request.get("output_format") or request.get("schema")
    return str(value) if isinstance(value, str) else None


def _chunk_texts(db: ScopedDB, chunk_ids: list[str]) -> dict[str, dict[str, Any]]:
    if not chunk_ids:
        return {}
    rows = db.fetch_all(
        "SELECT chunk_id, text, evidence_type, subject_entity_id, page, source_ref FROM chunks"
        " WHERE chunk_id = ANY(%s)",
        (chunk_ids,),
    )
    return {
        str(r[0]): {
            "text": r[1],
            "evidence_type": r[2],
            "subject_entity_id": r[3],
            "page": r[4],
            "source_ref": r[5],
        }
        for r in rows
    }


def check_call(
    db: ScopedDB,
    *,
    name: str,
    request: dict[str, Any],
    response: Any,
    prompt_version: str | None,
    model: str | None,
    nonce: str | None,
    full_user: str | None = None,
) -> ReplayCall:
    """Rebuild what the references allow (prompt, schema block, fenced evidence, and the
    whole user text when the caller can reproduce it) and diff against the stored request."""
    diff: list[str] = []
    system = _system_texts(request)
    if prompt_version:
        try:
            prompt = load_prompt(prompt_version)
        except FileNotFoundError:
            diff.append(f"prompt file for version {prompt_version!r} is missing")
        else:
            if not system:
                diff.append("stored request has no system blocks")
            elif system[0][0] != prompt.text:
                diff.append(
                    f"system[0] differs from prompt file {prompt_version} ({prompt.sha256[:12]})"
                )
    else:
        diff.append("no prompt version recorded")
    schema = _schema_name(request)
    if schema:
        model_cls = schema_model(schema)
        if model_cls is None:
            diff.append(f"schema {schema!r} is not registered")
        elif len(system) >= 2 and system[1][0] != schema_block(model_cls):
            diff.append(f"system[1] differs from the JSON schema block of {schema}")
    user = _user_text(request)
    fenced = 0
    if user is not None:
        blocks = prompt_guard.parse_fences(user)
        fenced = len(blocks)
        if nonce and blocks and any(b.nonce != nonce for b in blocks):
            diff.append("a fenced block carries a different nonce than the record")
        if nonce and blocks and nonce not in user:
            diff.append("the record's fence nonce does not appear in the stored request")
        chunks = _chunk_texts(db, [b.chunk_id for b in blocks])
        for block in blocks:
            chunk_id = block.chunk_id
            chunk = chunks.get(chunk_id)
            if chunk is None:
                diff.append(f"chunk {chunk_id} is no longer in the index")
                continue
            framed = prompt_guard.frame_evidence(
                [
                    FramedChunk(
                        chunk_id,
                        str(chunk["text"]),
                        str(chunk["evidence_type"]),
                        chunk["subject_entity_id"],
                        chunk["page"],
                        str(chunk["source_ref"] or ""),
                    )
                ],
                block.nonce,
            )
            rebuilt = prompt_guard.parse_fences(framed.text)
            if not rebuilt or rebuilt[0].body != block.body:
                diff.append(f"chunk {chunk_id}: the fenced text differs from the indexed text")
        if full_user is not None and full_user != user:
            diff.append("the rebuilt user text differs from the stored request")
    return ReplayCall(
        name=name,
        model=model,
        prompt_version=prompt_version,
        schema=schema,
        fenced_chunks=fenced,
        diff=tuple(diff),
        request=request,
        response=response,
    )


def _agent_calls(
    db: ScopedDB, request_json: dict[str, Any] | None, nonce: str | None
) -> tuple[ReplayCall, ...]:
    calls = (request_json or {}).get("calls") or {}
    out: list[ReplayCall] = []
    for name, entry in calls.items():
        if not isinstance(entry, dict) or not isinstance(entry.get("request"), dict):
            continue
        out.append(
            check_call(
                db,
                name=str(name),
                request=entry["request"],
                response=entry.get("response"),
                prompt_version=entry.get("prompt_version"),
                model=entry.get("model"),
                nonce=nonce,
            )
        )
    return tuple(out)


def find(db: ScopedDB, record_id: str) -> tuple[str, Any] | None:
    verdict = verdicts.load(db, record_id)
    if verdict is not None:
        return "verdict", verdict
    investigation = investigations.get(db, record_id)
    if investigation is not None:
        return "investigation", investigation
    report = policy_schema.get_report(db, record_id)
    if report is not None:
        return "gap_report", report
    answer = copilot_schema.get(db, record_id)
    if answer is not None:
        return "copilot_answer", answer
    return None


def rebuild(db: ScopedDB, settings: Settings, record_id: str) -> ReplayReport:
    found = find(db, record_id)
    if found is None:
        raise LookupError(f"no verdict, investigation, gap report, or copilot answer {record_id!r}")
    kind, record = found
    if kind == "verdict":
        verdict = record
        if verdict.tier == 1 or not verdict.request_json:
            return ReplayReport(kind, record_id, verdict.tenant_id, verdict.fence_nonce, ())
        if verdict.tier == 2:
            full_user: str | None = None
            spec = knowledge.load().tests.get(verdict.test_id)
            request = verdict.request_json
            if spec is not None and verdict.fence_nonce:
                ctx = load_context(
                    db, settings, spec, verdict.trigger_observed_at, verdict.created_at
                )
                _, description = build_query(ctx, spec, verdict.subject)
                stored_user = _user_text(request) or ""
                chunk_ids = [b.chunk_id for b in prompt_guard.parse_fences(stored_user)]
                chunks = _chunk_texts(db, chunk_ids)
                if all(c in chunks for c in chunk_ids):
                    framed = prompt_guard.frame_evidence(
                        [
                            FramedChunk(
                                cid,
                                str(chunks[cid]["text"]),
                                str(chunks[cid]["evidence_type"]),
                                chunks[cid]["subject_entity_id"],
                                chunks[cid]["page"],
                                str(chunks[cid]["source_ref"] or ""),
                            )
                            for cid in chunk_ids
                        ],
                        verdict.fence_nonce,
                    )
                    declaration = prompt_guard.fence_declaration(verdict.fence_nonce)
                    full_user = (
                        f"{declaration}\n\n{description}\n\n{framed.text}\n"
                        "Fill every field of the schema from the fenced evidence only."
                    )
            call = check_call(
                db,
                name="extract",
                request=request,
                response=verdict.response_json,
                prompt_version=verdict.prompt_version,
                model=verdict.model,
                nonce=verdict.fence_nonce,
                full_user=full_user,
            )
            return ReplayReport(kind, record_id, verdict.tenant_id, verdict.fence_nonce, (call,))
        return ReplayReport(
            kind,
            record_id,
            verdict.tenant_id,
            verdict.fence_nonce,
            _agent_calls(db, verdict.request_json, verdict.fence_nonce),
        )
    nonce = None
    request_json = record.request_json
    if kind == "copilot_answer":
        nonce = record.fence_nonce
    return ReplayReport(
        kind, record_id, record.tenant_id, nonce, _agent_calls(db, request_json, nonce)
    )


def _response_fields(response: Any) -> Any:
    if isinstance(response, dict):
        if "parsed" in response:
            return response["parsed"]
        if "parsed_output" in response:
            return response["parsed_output"]
    return response


def execute(
    db: ScopedDB, settings: Settings, record_id: str, llm: LLMClient
) -> list[tuple[str, list[str]]]:
    """Re-send every structured call of the record and diff the parsed responses. Chat calls
    (tool loops) are not re-executable because their tool results were live reads."""
    report = rebuild(db, settings, record_id)
    out: list[tuple[str, list[str]]] = []
    for call in report.calls:
        schema = call.schema
        user = _user_text(call.request)
        model = call.request.get("model") or call.model
        model_cls = schema_model(schema) if schema else None
        if model_cls is None or user is None or not model:
            out.append((call.name, ["not re-executable (a chat or tool call)"]))
            continue
        system = [SystemBlock(text, cache=cached) for text, cached in _system_texts(call.request)]
        extraction = llm.extract(
            schema=model_cls,
            system=system,
            user=user,
            model=str(model).removeprefix("stub:"),
            tracker=BudgetTracker(RunBudget.from_settings(settings)),
        )
        before = _response_fields(call.response)
        after = extraction.parsed.model_dump(mode="json")
        diff: list[str] = []
        if isinstance(before, dict):
            for key in sorted(set(before) | set(after)):
                if before.get(key) != after.get(key):
                    diff.append(
                        f"{key}: stored {json.dumps(before.get(key), default=str)[:120]}"
                        f" -> now {json.dumps(after.get(key), default=str)[:120]}"
                    )
        else:
            diff.append("stored response has no parsed fields to compare")
        out.append((call.name, diff))
    return out
