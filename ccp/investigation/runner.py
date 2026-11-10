"""The Investigator runner (spec.md Layer 14): the second consumer of the `verdicts` topic.
It runs the graph for every fail or needs_review verdict, persists the Investigation,
validates every proposed target, writes one remediation_proposals row per surviving
action, and opens one approval item per proposal plus one evidence_request when
evidence is missing. Compliance state never waits for it.

An investigation is a job (A-109):

    verdicts ──► investigator (ordered) ──► investigations (parallel, one job per verdict)
                 reads every verdict, queues        a lease, a heartbeat, parking without a
                 fail and needs_review; no model    key or budget; the agent runs outside any
                 call inside its transaction        transaction, on thread
                                                    <tenant>:investigate:<verdict>:run, so a
                                                    retried job resumes its own run and the
                                                    run log serves what it already did
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import psycopg

from ccp.agent import authz, core, threads
from ccp.agent.allowlists import ALLOWLISTS
from ccp.agent.graphs import investigator as graph_module
from ccp.agent.tools import ToolContext, bind_tools
from ccp.approvals import store as approvals
from ccp.config import Settings
from ccp.evaluators import budget_gate, targets
from ccp.evaluators import verdict as verdicts
from ccp.evaluators.tier2 import AITierUnavailable, Providers
from ccp.evaluators.verdict import Citation, Verdict
from ccp.ingest import event_log, ops
from ccp.ingest.consumers import OrderedConsumer, ParallelConsumer, ParkedError, TerminalError
from ccp.ingest.event_log import LogRecord
from ccp.ingest.worker import register
from ccp.investigation import schema
from ccp.investigation.schema import Investigation, MissingEvidence, RemediationProposal
from ccp.llm.budget import BudgetTracker, RunBudget
from ccp.observability import steps
from ccp.remediation import proposals as proposal_store
from ccp.storage import checkpoints
from ccp.storage.db import Pools, agent_pool
from ccp.storage.scoped import ScopedDB
from ccp.types import VerdictStatus

GRAPH = graph_module.GRAPH_NAME
INVESTIGATED_STATUSES = (VerdictStatus.failed, VerdictStatus.needs_review)
JOB_TOPIC = "investigations"


@dataclass
class RunSummary:
    consumed: int = 0  # verdict records read off the topic
    enqueued: int = 0  # investigation jobs queued
    investigated: int = 0
    skipped: int = 0
    parked: int = 0  # jobs held for a model key or the budget
    proposals: int = 0
    evidence_requests: int = 0
    rejected_targets: list[str] = field(default_factory=list)


def thread_id_of(db: ScopedDB, verdict_id: str) -> str:
    """One run per verdict, named by it, so a retried job finds its own run (A-109)."""
    return checkpoints.thread_id_for(db.tenant, "investigate", verdict_id, "run")


def _build(
    db: ScopedDB,
    settings: Settings,
    providers: Providers,
    now: datetime,
    run_id: str | None = None,
) -> tuple[Any, Any, BudgetTracker]:
    tracker = BudgetTracker(RunBudget.from_settings(settings))
    agent = agent_pool(settings)
    tools = bind_tools(
        ToolContext(  # read-only: the agent role (A-115)
            ScopedDB(agent, db.tenant), providers.embedder, providers.reranker, providers.retrieval
        ),
        tuple(sorted(ALLOWLISTS[GRAPH])),
        gate=authz.Gate(f"graph:{GRAPH}", run_id=run_id, log_db=db),
    )
    deps = core.ModelDeps(
        providers.llm, settings.investigator_model, tracker, reasoning=True, log_db=db
    )
    spec = graph_module.build_spec(db.tenant, tools, deps, now=now)
    pool = checkpoints.saver_pool(settings, db.tenant)
    saver = checkpoints.tenant_saver(pool, db.tenant)
    return core.build_graph(spec, saver), Pools(pool, agent), tracker


def investigate(
    db: ScopedDB,
    settings: Settings,
    verdict: Verdict,
    providers: Providers,
    now: datetime | None = None,
) -> Investigation:
    """Run the graph for one verdict, persist, propose, and open approval items."""
    if providers.llm is None:
        raise AITierUnavailable("ANTHROPIC_API_KEY is not set; the Investigator is unavailable")
    existing = schema.for_verdict(db, verdict.verdict_id)
    if existing is not None:
        return existing
    moment = now or datetime.now(UTC)
    budget_gate.check_daily_budget(db, moment)  # ParkedError past the cap
    thread_id = thread_id_of(db, verdict.verdict_id)
    previous = threads.get(db, thread_id)
    if previous is None:
        threads.create(db, thread_id, GRAPH, verdict.test_id, verdict.subject_key)
    else:  # a retried job: continue its own run from the last checkpoint
        threads.mark_running(db, thread_id)
    graph, pool, tracker = _build(db, settings, providers, moment, run_id=thread_id)
    config = {"configurable": {"thread_id": thread_id}}
    initial: dict[str, Any] = {
        "tenant_id": db.tenant.tenant_id,
        "test_id": verdict.test_id,
        "subject_key": verdict.subject_key,
        "verdict_id": verdict.verdict_id,
        "thread_id": thread_id,
        "now": moment.isoformat(),
        "model": settings.investigator_model,
        "retrieved_chunks": {},
        "evidence_coverage": {},
        "extracted_raw": {},
        "extracted": {},
        "requests": {},
        "context": {},
    }
    threads.set_input(db, thread_id, initial)
    trajectory: list[str] = list(previous.trajectory) if previous is not None else []
    try:
        with steps.step(
            "investigate.run",
            layer="investigation",
            tenant=db.tenant,
            inputs={"verdict_id": verdict.verdict_id, "thread_id": thread_id},
        ) as s:
            checkpointed = previous is not None and bool(graph.get_state(config).values)
            if checkpointed:
                s.event(f"resumed_thread:{thread_id}")
            payload = None if checkpointed else initial  # resume: only what is left runs
            for update in graph.stream(payload, config, stream_mode="updates", durability="sync"):
                trajectory.extend(str(k) for k in update)
            final = dict(graph.get_state(config).values.get("final") or {})
            if not final:
                raise RuntimeError("investigator graph finished without a result")
            investigation = _finish(db, verdict, final, s)
            threads.mark_finished(db, thread_id, verdict.verdict_id, trajectory)
            budget_gate.record_spend(db, tracker.usd, tracker.tokens, s.step_id, moment)
            s.set_output(
                {"investigation_id": investigation.investigation_id, "trajectory": trajectory}
            )
            return investigation
    except Exception as exc:
        threads.mark_failed(db, thread_id, f"{type(exc).__name__}: {exc}", trajectory)
        raise
    finally:
        pool.close()


def _finish(
    db: ScopedDB, verdict: Verdict, final: dict[str, Any], s: steps.StepRecord
) -> Investigation:
    events = list(final.get("guardrail_events") or [])
    bounded: list[RemediationProposal] = []
    for item in final.get("proposed_actions") or []:
        action = str(item["action"])
        target = {str(k): str(v) for k, v in dict(item.get("target") or {}).items()}
        resolution = targets.resolve(db, verdict, action, target)  # the runner validates again
        if not resolution.ok:
            events.extend(resolution.events)
            for event in resolution.events:
                s.event(event)
            continue
        bounded.append(
            RemediationProposal(
                action=action,
                target=target,
                rationale=str(item.get("rationale", "")),
                reversible=bool(item["reversible"]),
                live_capable=bool(item["live_capable"]),
            )
        )
    missing = tuple(MissingEvidence.model_validate(m) for m in final.get("missing_evidence") or [])
    written: list[RemediationProposal] = []
    for proposal in bounded:
        row = _proposal_row(db, verdict, proposal)
        written.append(proposal.model_copy(update={"proposal_id": row.proposal_id}))
    investigation = Investigation(
        tenant_id=db.tenant.tenant_id,
        verdict_id=verdict.verdict_id,
        root_cause=str(final.get("root_cause") or ""),
        root_cause_category=final.get("root_cause_category") or "unknown",
        missing_evidence=missing,
        proposed_actions=tuple(written),
        citations=tuple(Citation.model_validate(c) for c in final.get("citations") or []),
        confidence=float(final.get("confidence") or 0.0),
        confidence_cap=float(final.get("confidence_cap") or 1.0),
        model=final.get("model"),
        prompt_version=str(final.get("prompt_version") or schema.PROMPT_VERSION),
        guardrail_events=tuple(events),
        thread_id=final.get("thread_id"),
        request_json=final.get("request_json"),
        response_json=final.get("response_json"),
    )
    schema.persist(db, investigation)
    if missing:
        approvals.open_item(
            db,
            "evidence_request",
            investigation.investigation_id,
            {
                "verdict_id": verdict.verdict_id,
                "test_id": verdict.test_id,
                "subject_key": verdict.subject_key,
                "missing_evidence": [m.model_dump(mode="json") for m in missing],
            },
        )
    return investigation


def _proposal_row(db: ScopedDB, verdict: Verdict, proposal: RemediationProposal) -> Any:
    """One remediation_proposals row per action; reuse a rule-fallback row for the same
    action and target rather than opening a second approval for the same change."""
    for existing in proposal_store.for_verdict(db, verdict.verdict_id):
        if existing.action == proposal.action and existing.target == proposal.target:
            return existing
    return proposal_store.create(
        db,
        verdict_id=verdict.verdict_id,
        source="investigator",
        action=proposal.action,
        target=proposal.target,
        rationale=proposal.rationale,
        reversible=proposal.reversible,
        live_capable=proposal.live_capable,
    )


class InvestigationsConsumer(OrderedConsumer):
    """Second consumer of the verdicts topic: queues one investigation job per fail or
    needs_review verdict that has none (A-109). It makes no model call, so it never parks
    and never holds its batch open across one."""

    name = "investigator"
    topic = "verdicts"

    def __init__(
        self,
        db: ScopedDB,
        settings: Settings,
        providers: Providers | None = None,
        summary: RunSummary | None = None,
    ) -> None:
        super().__init__(db)
        self.settings = settings
        self.summary = summary if summary is not None else RunSummary()

    def handle(self, cur: psycopg.Cursor[Any], record: LogRecord) -> None:
        self.summary.consumed += 1
        verdict = verdicts.load(self._db, record.ref)
        if verdict is None or verdict.status not in INVESTIGATED_STATUSES:
            self.summary.skipped += 1
            return
        if schema.for_verdict(self._db, verdict.verdict_id) is not None:
            self.summary.skipped += 1
            return
        key = f"{verdict.tenant_id}:{verdict.verdict_id}"
        queued = cur.execute(
            "SELECT 1 FROM event_log WHERE topic = %s AND partition_key = %s"
            " AND status <> 'dead' LIMIT 1",
            (JOB_TOPIC, key),
        ).fetchone()
        if queued is not None:  # a redelivered verdict: its job is already queued
            self.summary.skipped += 1
            return
        event_log.append(
            cur,
            tenant_id=verdict.tenant_id,
            topic=JOB_TOPIC,
            partition_key=key,
            source_ts=verdict.valid_from,
            ref=verdict.verdict_id,
        )
        self.summary.enqueued += 1


class InvestigationJobs(ParallelConsumer):
    """Runs one investigation per job, outside any transaction (A-109): parks without a
    model key or past the budget, and a retried job resumes the verdict's own run."""

    name = "investigations"
    topic = JOB_TOPIC
    one_per_key = True  # one run per verdict at a time

    def __init__(
        self,
        db: ScopedDB,
        settings: Settings,
        providers: Providers | None = None,
        summary: RunSummary | None = None,
    ) -> None:
        super().__init__(
            db,
            max_attempts=settings.ccp_max_attempts,
            visibility_seconds=settings.ccp_claim_visibility_seconds,
            base_delay=settings.ccp_retry_base_seconds,
        )
        self.settings = settings
        self._providers = providers
        self.summary = summary if summary is not None else RunSummary()

    @property
    def providers(self) -> Providers:
        if self._providers is None:
            self._providers = Providers.from_settings(self.settings)
        return self._providers

    def handle(self, record: LogRecord) -> None:
        verdict = verdicts.load(self._db, record.ref)
        if verdict is None:
            raise TerminalError(f"verdict {record.ref!r} is not stored")
        if schema.for_verdict(self._db, verdict.verdict_id) is not None:
            self.summary.skipped += 1
            return
        if self.providers.llm is None:
            self.summary.parked += 1
            raise ParkedError("ANTHROPIC_API_KEY is not set; the Investigator is unavailable")
        try:
            investigation = investigate(self._db, self.settings, verdict, self.providers)
        except ParkedError:
            self.summary.parked += 1
            raise
        self.summary.investigated += 1
        self.summary.proposals += len(investigation.proposed_actions)
        self.summary.evidence_requests += 1 if investigation.missing_evidence else 0
        self.summary.rejected_targets.extend(
            e for e in investigation.guardrail_events if e.startswith("target_rejected:")
        )


register("investigator")(lambda db, settings: InvestigationsConsumer(db, settings))
register("investigations")(lambda db, settings: InvestigationJobs(db, settings))


def drain(db: ScopedDB, settings: Settings, providers: Providers | None = None) -> RunSummary:
    """Queue the jobs for every new fail and needs_review verdict, then run them. Jobs
    parked for want of a model key go back to pending when one is configured; without a key
    they park again (A-109)."""
    summary = RunSummary()
    queue = InvestigationsConsumer(db, settings, providers, summary)
    while queue.run_once():
        pass
    jobs = InvestigationJobs(db, settings, providers, summary)
    if jobs.providers.llm is not None:
        ops.unpark(db, JOB_TOPIC)
    while jobs.run_once():
        pass
    return summary
