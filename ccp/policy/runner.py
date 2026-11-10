"""The Policy runner (spec.md Layer 15): builds the graph for one tenant and control, runs it in
gap or draft mode, persists the GapReport, and in draft mode writes the Markdown under
data/drafts/<tenant>/ and opens a policy_draft approval item. Graphs read, runners write;
the draft never becomes evidence on its own."""

from __future__ import annotations

import hashlib
import re
from datetime import UTC, datetime
from typing import Any

from ulid import ULID

from ccp.agent import authz, core, threads
from ccp.agent.allowlists import ALLOWLISTS
from ccp.agent.graphs import policy as graph_module
from ccp.agent.tools import ToolContext, bind_tools
from ccp.approvals import store as approvals
from ccp.config import Settings
from ccp.evaluators import budget_gate
from ccp.evaluators.tier2 import AITierUnavailable, Providers
from ccp.evaluators.verdict import Citation
from ccp.knowledge import graph as knowledge
from ccp.llm.budget import BudgetTracker, RunBudget
from ccp.observability import steps
from ccp.policy import schema
from ccp.policy.schema import GapReport, PolicyDraft, PolicyGap
from ccp.storage import checkpoints
from ccp.storage.scoped import ScopedDB

GRAPH = graph_module.GRAPH_NAME
_SAFE = re.compile(r"[^A-Za-z0-9._-]+")


GENERIC_NAME_WORDS = frozenset(
    {
        "corporation",
        "corp",
        "company",
        "incorporated",
        "limited",
        "holdings",
        "group",
        "international",
        "global",
        "technologies",
        "technology",
        "systems",
        "services",
        "solutions",
        "industries",
        "enterprises",
        "partners",
        "example",
    }
)  # words of a company name that name nobody in particular; never scrubbed on their own


def other_tenant_tokens(settings: Settings, tenant_id: str) -> list[str]:
    """Distinctive names of every *other* configured tenant (config, not tenant data): a draft
    for one tenant must never contain them (S9). The whole display name, the GitHub org and
    the id always count; a single word of the name counts only when it is distinctive."""
    tokens: list[str] = []
    for other_id, other in settings.load_tenants().items():
        if other_id == tenant_id:
            continue
        tokens.extend([other.display_name, other.github_org, other_id])
        tokens.extend(
            part
            for part in other.display_name.split()
            if len(part) > 4 and part.lower() not in GENERIC_NAME_WORDS
        )
    return sorted({t for t in tokens if t}, key=len, reverse=True)


def _build(
    db: ScopedDB,
    settings: Settings,
    providers: Providers,
    now: datetime,
    run_id: str | None = None,
) -> tuple[Any, Any, BudgetTracker]:
    tracker = BudgetTracker(RunBudget.from_settings(settings))
    tools = bind_tools(
        ToolContext(db, providers.embedder, providers.reranker, providers.retrieval),
        tuple(sorted(ALLOWLISTS[GRAPH])),
        fixed={"search_evidence": {"evidence_type": "policy_document"}},
        gate=authz.Gate(f"graph:{GRAPH}", run_id=run_id, log_db=db),
    )
    deps = core.ModelDeps(providers.llm, settings.policy_model, tracker, reasoning=True, log_db=db)
    spec = graph_module.build_spec(
        db.tenant,
        tools,
        deps,
        now=now,
        forbidden_tokens=other_tenant_tokens(settings, db.tenant.tenant_id),
    )
    pool = checkpoints.saver_pool(settings, db.tenant)
    saver = checkpoints.tenant_saver(pool, db.tenant)
    return core.build_graph(spec, saver), pool, tracker


def _run(
    db: ScopedDB,
    settings: Settings,
    control_id: str,
    providers: Providers,
    mode: str,
    now: datetime,
) -> tuple[dict[str, Any], list[str]]:
    if providers.llm is None:
        raise AITierUnavailable("ANTHROPIC_API_KEY is not set; the Policy Agent is unavailable")
    if control_id not in knowledge.load().controls:
        raise KeyError(f"unknown control {control_id!r}")
    budget_gate.check_daily_budget(db, now)  # ParkedError past the cap, before any row exists
    thread_id = checkpoints.thread_id_for(db.tenant, f"policy-{mode}", control_id, str(ULID()))
    graph, pool, tracker = _build(db, settings, providers, now, run_id=thread_id)
    threads.create(db, thread_id, GRAPH, "policy", control_id)
    config = {"configurable": {"thread_id": thread_id}}
    initial: dict[str, Any] = {
        "tenant_id": db.tenant.tenant_id,
        "subject_key": control_id,
        "thread_id": thread_id,
        "mode": mode,
        "now": now.isoformat(),
        "model": settings.policy_model,
        "retrieved_chunks": {},
        "evidence_coverage": {},
        "extracted_raw": {},
        "extracted": {},
        "requests": {},
        "context": {},
    }
    trajectory: list[str] = []
    try:
        with steps.step(
            f"policy.{mode}",
            layer="policy",
            tenant=db.tenant,
            inputs={"control_id": control_id, "thread_id": thread_id},
        ) as s:
            for update in graph.stream(initial, config, stream_mode="updates", durability="sync"):
                trajectory.extend(str(k) for k in update)
            final = dict(graph.get_state(config).values.get("final") or {})
            if not final:
                raise RuntimeError("policy graph finished without a result")
            threads.mark_finished(db, thread_id, control_id, trajectory)
            budget_gate.record_spend(db, tracker.usd, tracker.tokens, s.step_id, now)
            s.set_output(
                {
                    "trajectory": trajectory,
                    "uncovered": [
                        g["requirement_id"] for g in final.get("gaps", []) if not g.get("covered")
                    ],
                }
            )
            return final, trajectory
    except Exception as exc:
        threads.mark_failed(db, thread_id, f"{type(exc).__name__}: {exc}", trajectory)
        raise
    finally:
        pool.close()


def _report_from(db: ScopedDB, control_id: str, final: dict[str, Any]) -> GapReport:
    gaps = tuple(
        PolicyGap(
            requirement_id=str(g["requirement_id"]),
            covered=bool(g.get("covered")),
            covering_citations=tuple(
                Citation.model_validate(c) for c in g.get("covering_citations") or []
            ),
            gap_description=g.get("gap_description"),
        )
        for g in final.get("gaps") or []
    )
    return GapReport(
        tenant_id=db.tenant.tenant_id,
        control_id=control_id,
        gaps=gaps,
        summary=str(final.get("summary") or ""),
        confidence=float(final.get("confidence") or 0.0),
        confidence_cap=float(final.get("confidence_cap") or 1.0),
        model=final.get("model"),
        prompt_version=str(final.get("prompt_version") or ""),
        guardrail_events=tuple(final.get("guardrail_events") or []),
        thread_id=final.get("thread_id"),
        request_json=final.get("request_json"),
        response_json=final.get("response_json"),
    )


def gap(
    db: ScopedDB,
    settings: Settings,
    control_id: str,
    providers: Providers,
    now: datetime | None = None,
) -> GapReport:
    final, _ = _run(db, settings, control_id, providers, "gap", now or datetime.now(UTC))
    report = _report_from(db, control_id, final)
    schema.persist_report(db, report)
    return report


def draft(
    db: ScopedDB,
    settings: Settings,
    control_id: str,
    providers: Providers,
    now: datetime | None = None,
) -> tuple[GapReport, PolicyDraft]:
    moment = now or datetime.now(UTC)
    final, _ = _run(db, settings, control_id, providers, "draft", moment)
    report = _report_from(db, control_id, final)
    schema.persist_report(db, report)
    markdown = str(final.get("markdown") or "")
    if not markdown.strip():
        raise RuntimeError("the compose step produced no draft")
    scrub_events = tuple(e for e in report.guardrail_events if e.startswith("draft_"))
    directory = settings.drafts_dir / db.tenant.tenant_id
    directory.mkdir(parents=True, exist_ok=True)
    draft_id = str(ULID())
    stamp = moment.strftime("%Y%m%dT%H%M%SZ")
    path = directory / f"{_SAFE.sub('-', control_id)}-{stamp}-{draft_id}.md"
    with path.open("xb") as fh:  # never overwrite an earlier draft; LF bytes everywhere,
        fh.write(markdown.encode("utf-8"))  # so the recorded hash is the file's hash
    digest = hashlib.sha256(markdown.encode("utf-8")).hexdigest()
    approval_id = approvals.open_item(
        db,
        "policy_draft",
        draft_id,
        {
            "control_id": control_id,
            "report_id": report.report_id,
            "path": str(path),
            "markdown_sha256": digest,
            "uncovered": list(report.uncovered),
        },
    )
    result = PolicyDraft(
        draft_id=draft_id,
        tenant_id=db.tenant.tenant_id,
        control_id=control_id,
        based_on_report_id=report.report_id,
        markdown=markdown,
        addressed_requirements=tuple(report.uncovered),
        path=str(path),
        approval_id=approval_id,
        scrub_events=scrub_events,
        created_at=moment,
    )
    schema.persist_draft(db, result, digest)
    return report, result
