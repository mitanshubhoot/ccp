"""Service playbooks for the gaps in coverage (spec.md A-167).

For every resource and intent the coverage map lists as a gap (A-166), one fixed sequence
per class, chosen by code:

    read     the resource's configuration from AWS Config, read-only, its responses landed
             as raw evidence first
    known    a field listed for the type and intent (ccp/knowledge/playbook_fields.yaml)
             is decided by code
    unknown  otherwise one tool-less model call reads the configuration (a fenced
             document, invisible characters stripped) and cites JSON Pointers with the
             values found; code resolves every pointer and compares every value
    decide   fail when code's field or a fully checked citation shows the intent unmet;
             needs_review when a citation does not check, the model cannot tell, or the
             configuration cannot be read; nothing at all when nothing is wrong

A playbook never passes a resource: that needs a deterministic test, and the gap stays a
gap until the drafting pipeline (A-165) turns a reviewed mapping into one. Runs are bounded:
a daily cap of reads per tenant, a budget per model call, and a kill switch per class
(`ccp_playbooks_disabled`, or "all").
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field
from ulid import ULID

from ccp.config import Settings
from ccp.connectors.aws.connector import resource_config
from ccp.evaluators import jobs
from ccp.evaluators.extraction import schema_block
from ccp.ingest.landing import land_received
from ccp.ingest.raw_zone import open_zone
from ccp.knowledge.graph import KNOWLEDGE_DIR
from ccp.llm.budget import BudgetTracker, RunBudget
from ccp.llm.client import LLMClient, SystemBlock, try_build_client
from ccp.llm.prompts import load_prompt
from ccp.lookups import coverage
from ccp.lookups.runner import Connectors, default_connectors
from ccp.retrieval.segments import resolve_pointer
from ccp.security import prompt_guard
from ccp.storage.scoped import ScopedDB

PROMPT_VERSION = "playbook_v1"
FIELDS_FILE = KNOWLEDGE_DIR / "playbook_fields.yaml"


class CitedPath(BaseModel):
    model_config = ConfigDict(extra="forbid")

    pointer: str
    value_json: str


class PlaybookReading(BaseModel):
    model_config = ConfigDict(extra="forbid")

    finding: Literal["fail", "unclear", "no_issue"]
    paths: list[CitedPath] = Field(max_length=6)
    reason: str = Field(max_length=300)


@lru_cache(maxsize=2)
def known_fields(path: Path = FIELDS_FILE) -> dict[str, dict[str, dict[str, Any]]]:
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return {str(t): {str(i): dict(v) for i, v in (m or {}).items()} for t, m in raw.items()}


def meets(value: Any, expect: dict[str, Any]) -> bool:
    if "equals" in expect:
        return bool(value == expect["equals"])
    if "truthy" in expect:
        return bool(value) is bool(expect["truthy"])
    return (value is not None) is bool(expect.get("exists", True))


@dataclass
class Read:
    status: Literal["fail", "needs_review"] | None
    reason: str
    paths: list[dict[str, Any]] = field(default_factory=list)
    model: str | None = None


def by_code(config: dict[str, Any], spec: dict[str, Any]) -> Read:
    try:
        value = resolve_pointer(config, str(spec["pointer"]))
    except KeyError:
        value = None
    if meets(value, dict(spec["expect"])):
        return Read(None, "the listed field meets the intent")
    return Read(
        "fail",
        f"{spec['pointer']} is {json.dumps(value)}; expected {json.dumps(spec['expect'])}",
        [{"pointer": spec["pointer"], "value": value, "checked": True}],
    )


def by_model(
    llm: LLMClient,
    settings: Settings,
    config: dict[str, Any],
    resource: dict[str, str],
    cls: str,
    intent: str,
) -> Read:
    text, _ = prompt_guard.strip_invisible(json.dumps(config, default=str)[:20000])
    nonce = prompt_guard.new_nonce()
    block = prompt_guard.frame_evidence(
        [
            prompt_guard.FramedChunk(
                chunk_id=f"{resource['resource_type']}:{resource['resource_id']}",
                text=text,
                evidence_type="cloud_configuration",
            )
        ],
        nonce,
    )
    result = llm.extract(
        schema=PlaybookReading,
        system=[
            SystemBlock(load_prompt(PROMPT_VERSION).text),
            SystemBlock(schema_block(PlaybookReading), cache=True),
        ],
        user=(
            f"{prompt_guard.fence_declaration(nonce)}\n\nResource type"
            f" {resource['resource_type']}, class {cls}, intent {intent}."
        ),
        model=settings.ccp_playbook_model,
        tracker=BudgetTracker(
            RunBudget.from_settings(settings).lowered(max_seconds=60, max_usd=0.10)
        ),
        max_tokens=1024,
        documents=[block.text],
    )
    reading: PlaybookReading = result.parsed
    if reading.finding == "no_issue":
        return Read(None, reading.reason, model=result.model)
    if reading.finding == "unclear":
        return Read(
            "needs_review", f"the playbook could not tell: {reading.reason}", model=result.model
        )
    checked: list[dict[str, Any]] = []
    for cited in reading.paths:
        try:
            found = resolve_pointer(config, cited.pointer)
            claimed = json.loads(cited.value_json)
            ok = found == claimed
        except (KeyError, ValueError):
            found, ok = None, False
        checked.append({"pointer": cited.pointer, "value": found, "checked": ok})
    if not checked or not all(c["checked"] for c in checked):
        return Read(
            "needs_review",
            f"a cited path did not check, so a person reads it: {reading.reason}",
            checked,
            result.model,
        )
    return Read("fail", reading.reason, checked, result.model)


@dataclass
class Summary:
    read: int = 0
    failed: int = 0
    needs_review: int = 0
    nothing_wrong: int = 0
    skipped: list[str] = field(default_factory=list)


def _today_reads(db: ScopedDB) -> int:
    row = db.fetch_one(
        "SELECT count(*) FROM raw_responses WHERE sync_run_id LIKE 'playbook:%%'"
        " AND received_at >= date_trunc('day', clock_timestamp())"
    )
    return int(row[0]) if row else 0


def run(
    db: ScopedDB,
    settings: Settings,
    connectors: Connectors | None = None,
    llm: LLMClient | None = None,
) -> Summary:
    """Read every gap once a day, within the tenant's cap; record fail or needs_review."""
    summary = Summary()
    disabled = set(settings.ccp_playbooks_disabled)
    if "all" in disabled:
        summary.skipped.append("all playbooks are switched off")
        return summary
    aws = (connectors or default_connectors(db, settings))("aws")
    if aws is None:
        summary.skipped.append("no live AWS connection")
        return summary
    model = llm if llm is not None else try_build_client(settings)
    fields = known_fields()
    budget = settings.ccp_playbook_daily_cap - _today_reads(db)
    gaps = [r for r in coverage.build(db) if r.status.startswith("gap") and r.intent is not None]
    configs: dict[tuple[str, str], tuple[dict[str, Any] | None, list[str]]] = {}
    for row in gaps:
        assert row.intent is not None and row.service_class is not None
        if row.service_class in disabled:
            summary.skipped.append(f"{row.service_class} switched off")
            continue
        key = (row.resource_type, row.resource_id)
        if key not in configs:
            if budget <= 0:
                summary.skipped.append(f"daily cap: {row.resource_type} {row.resource_id}")
                continue
            budget -= 1
            try:
                found = (
                    resource_config(aws, *key)
                    if hasattr(aws, "_assumed")
                    else aws.resource_config(*key)
                )
            except Exception:  # an unreadable configuration is set aside for a person
                found = None
            ids = land_received(
                db,
                open_zone(settings),
                "aws",
                list(getattr(aws, "responses", ())),
                f"playbook:{row.service_class}",
            )
            configs[key] = (found, ids)
            summary.read += 1
        config, ids = configs[key]
        resource = {"resource_type": row.resource_type, "resource_id": row.resource_id}
        spec = fields.get(row.resource_type, {}).get(row.intent)
        if config is None:
            read = Read("needs_review", "the configuration could not be read")
        elif spec is not None:
            read = by_code(config, spec)
        elif model is not None:
            read = by_model(model, settings, config, resource, row.service_class, row.intent)
        else:
            read = Read("needs_review", "no listed field and no model: escalated to a person")
        if read.status is None:
            summary.nothing_wrong += 1
            if row.status == "gap":
                continue
            read = Read(None, f"no longer seen: {read.reason}")  # cleared, still not a pass
        else:
            summary.failed += read.status == "fail"
            summary.needs_review += read.status == "needs_review"
        db.execute(
            "INSERT INTO playbook_results (tenant_id, result_id, account, resource_type,"
            " resource_id, service_class, intent, status, reason, paths, playbook, model,"
            " response_ids) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (
                db.tenant.tenant_id,
                str(ULID()),
                row.account,
                row.resource_type,
                row.resource_id,
                row.service_class,
                row.intent,
                read.status or "cleared",
                read.reason,
                json.dumps(read.paths, default=str),
                f"{row.service_class}:{PROMPT_VERSION}",
                read.model,
                json.dumps(ids),
            ),
        )
    return summary


jobs.JOBS["playbooks"] = lambda db, settings, slot: run(db, settings).read
