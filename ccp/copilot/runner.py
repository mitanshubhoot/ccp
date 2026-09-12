"""The Copilot runner (spec.md Layer 17): one REPL session is one checkpointed thread; every
turn runs the graph, persists a CopilotAnswer, and validates any requested action against the
tenant's data. Nothing is opened until the operator confirms: then the Investigator runs for
the referenced verdict when no investigation exists, and each action becomes a remediation
proposal with its approval item, exactly where an Investigator run ends."""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import UTC, datetime
from types import TracebackType
from typing import Any, cast

from langgraph.checkpoint.postgres import PostgresSaver
from ulid import ULID

from ccp.agent import core, threads
from ccp.agent.allowlists import ALLOWLISTS
from ccp.agent.graphs import copilot as graph_module
from ccp.agent.tools import ToolContext, bind_tools
from ccp.approvals import store as approvals
from ccp.config import Settings
from ccp.copilot import schema
from ccp.copilot.schema import CopilotAnswer
from ccp.evaluators import targets
from ccp.evaluators import verdict as verdicts
from ccp.evaluators.tier2 import AITierUnavailable, Providers
from ccp.evaluators.verdict import Citation, Verdict
from ccp.investigation import runner as investigator
from ccp.investigation import schema as investigations
from ccp.knowledge import graph as knowledge
from ccp.llm.budget import BudgetTracker, RunBudget
from ccp.observability import steps
from ccp.remediation import proposals
from ccp.storage import checkpoints
from ccp.storage.scoped import ScopedDB

GRAPH = graph_module.GRAPH_NAME


@dataclass(frozen=True)
class PendingAction:
    """A requested action whose target resolved against this tenant's data; not yet opened."""

    action: str
    target: dict[str, str]
    rationale: str
    verdict_id: str
    reversible: bool
    live_capable: bool


@dataclass(frozen=True)
class Turn:
    answer: CopilotAnswer
    pending: tuple[PendingAction, ...]
    rejected: tuple[str, ...]  # guardrail events for actions that failed validation
    trajectory: tuple[str, ...]


@dataclass(frozen=True)
class Opened:
    approval_ids: tuple[str, ...]
    proposal_ids: tuple[str, ...]
    investigation_ids: tuple[str, ...]


def describe(turn: Turn) -> str:
    """Exactly what a confirmation would open, for the operator to read before saying yes."""
    if not turn.pending:
        return "No action was requested."
    lines = [f"Would open {len(turn.pending)} approval item(s):"]
    for p in turn.pending:
        where = ", ".join(f"{k}={v}" for k, v in sorted(p.target.items())) or "no target"
        kind = "reversible" if p.reversible else "not reversible"
        lines.append(f"- {p.action} ({where}); {kind}; fixes verdict {p.verdict_id}")
        if p.rationale:
            lines.append(f"    {p.rationale}")
    lines.append("Nothing runs until a reviewer approves and the remediation gates pass.")
    return "\n".join(lines)


class Session:
    """One REPL session: one tenant, one checkpoint thread, any number of turns."""

    def __init__(
        self,
        db: ScopedDB,
        settings: Settings,
        providers: Providers,
        *,
        session_id: str | None = None,
    ) -> None:
        if providers.llm is None:
            raise AITierUnavailable("ANTHROPIC_API_KEY is not set; the Copilot is unavailable")
        self.db = db
        self.settings = settings
        self.providers = providers
        self.config = graph_module.load_config(settings.ccp_copilot_config)
        self.session_id = session_id or str(ULID())
        self.thread_id = checkpoints.thread_id_for(
            db.tenant, "copilot", self.session_id, str(ULID())
        )
        self.known_tests = tuple(sorted(knowledge.load().tests))
        self.pool = checkpoints.saver_pool(settings, db.tenant)
        self.turn_count = 0
        self.trajectory: list[str] = []
        threads.create(db, self.thread_id, GRAPH, "copilot", self.session_id)

    def __enter__(self) -> Session:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        self.pool.close()

    def _graph(self, now: datetime) -> Any:
        tracker = BudgetTracker(RunBudget.from_settings(self.settings))
        tools = bind_tools(
            ToolContext(
                self.db, self.providers.embedder, self.providers.reranker, self.providers.retrieval
            ),
            tuple(sorted(ALLOWLISTS[GRAPH])),
        )
        deps = core.ModelDeps(
            self.providers.llm, self.settings.copilot_model, tracker, reasoning=True
        )
        spec = graph_module.build_spec(
            self.db.tenant,
            tools,
            deps,
            now=now,
            config=self.config,
            known_tests=self.known_tests,
        )
        return core.build_graph(spec, PostgresSaver(cast(Any, self.pool)))

    def ask(self, question: str, now: datetime | None = None) -> Turn:
        moment = now or datetime.now(UTC)
        self.turn_count += 1
        graph = self._graph(moment)
        config = {"configurable": {"thread_id": self.thread_id}}
        before: dict[str, Any] = (
            dict(graph.get_state(config).values or {}) if self.turn_count > 1 else {}
        )
        n_events = len(before.get("guardrail_events") or [])
        n_log = len(before.get("tool_log") or [])
        initial: dict[str, Any] = {
            "tenant_id": self.db.tenant.tenant_id,
            "thread_id": self.thread_id,
            "question": question,
            "turn": self.turn_count,
            "now": moment.isoformat(),
            "model": self.settings.copilot_model,
            "retrieved_chunks": {},
            "evidence_coverage": {},
            "extracted_raw": {},
            "extracted": {},
            "requests": {},
        }
        started = time.perf_counter()
        turn_nodes: list[str] = []
        with steps.step(
            "copilot.turn",
            layer="copilot",
            tenant=self.db.tenant,
            inputs={
                "thread_id": self.thread_id,
                "turn": self.turn_count,
                "question": question[:500],
            },
        ) as s:
            try:
                for update in graph.stream(initial, config, stream_mode="updates"):
                    turn_nodes.extend(str(k) for k in update)
            except Exception as exc:
                threads.mark_failed(
                    self.db,
                    self.thread_id,
                    f"{type(exc).__name__}: {exc}",
                    self.trajectory + turn_nodes,
                )
                raise
            self.trajectory.extend(turn_nodes)
            values = graph.get_state(config).values
            final = dict(values.get("final") or {})
            if not final:
                raise RuntimeError("copilot graph finished without an answer")
            events = list(values.get("guardrail_events") or [])[n_events:]
            tool_log = list(values.get("tool_log") or [])[n_log:]
            latency_ms = (time.perf_counter() - started) * 1000
            answer = CopilotAnswer(
                tenant_id=self.db.tenant.tenant_id,
                thread_id=self.thread_id,
                turn=self.turn_count,
                question=question,
                intent=str(final.get("intent") or "other"),
                text=str(final.get("text") or ""),
                citations=tuple(Citation.model_validate(c) for c in final.get("citations") or []),
                requested_actions=tuple(final.get("requested_actions") or []),
                model=final.get("model"),
                prompt_version=str(final.get("prompt_version") or ""),
                guardrail_events=tuple(events),
                latency_ms=latency_ms,
                fence_nonce=final.get("fence_nonce"),
                request_json={**(final.get("request_json") or {}), "tool_log": tool_log},
                response_json=final.get("response_json"),
                created_at=moment,
            )
            schema.persist(self.db, answer)
            threads.mark_finished(self.db, self.thread_id, answer.answer_id, self.trajectory)
            pending, rejected = self._validate(final.get("requested_actions") or [], final)
            for event in rejected:
                s.event(event)
            s.set_output(
                {
                    "intent": answer.intent,
                    "trajectory": turn_nodes,
                    "tools": [t.get("tool") for t in tool_log],
                    "pending": len(pending),
                    "latency_ms": round(latency_ms, 1),
                }
            )
        return Turn(answer, tuple(pending), tuple(rejected), tuple(turn_nodes))

    def _verdict_for(self, item: dict[str, Any], final: dict[str, Any]) -> Verdict | None:
        verdict_id = item.get("verdict_id")
        if verdict_id:
            found = verdicts.load(self.db, str(verdict_id))
            if found is not None:
                return found
        subject = str((item.get("target") or {}).get("subject_key") or "")
        test_id = final.get("test_id")
        if subject and "|" in subject:
            rows = [v for v in verdicts.latest_per_subject(self.db) if v.subject_key == subject]
            return rows[0] if rows else None
        if test_id:
            rows = verdicts.latest_per_subject(self.db, str(test_id))
            if subject:
                rows = [v for v in rows if v.subject == subject]
            return rows[0] if len(rows) == 1 else None
        return None

    def _validate(
        self, actions: list[dict[str, Any]], final: dict[str, Any]
    ) -> tuple[list[PendingAction], list[str]]:
        pending: list[PendingAction] = []
        rejected: list[str] = []
        for item in actions:
            action = str(item.get("action", ""))
            target = {str(k): str(v) for k, v in (item.get("target") or {}).items()}
            verdict = self._verdict_for(item, final)
            if verdict is None:
                rejected.append(f"action_rejected:unknown_verdict:{action}")
                continue
            resolution = targets.resolve(self.db, verdict, action, target)
            if not resolution.ok:
                rejected.extend(
                    list(resolution.events) or [f"action_rejected:{resolution.reason}:{action}"]
                )
                continue
            pending.append(
                PendingAction(
                    action=action,
                    target=target,
                    rationale=str(item.get("rationale", "")),
                    verdict_id=verdict.verdict_id,
                    reversible=bool(item.get("reversible")),
                    live_capable=bool(item.get("live_capable")),
                )
            )
        return pending, rejected

    def _proposal_row(self, verdict: Verdict, p: PendingAction) -> Any:
        for existing in proposals.for_verdict(self.db, verdict.verdict_id):
            if existing.action == p.action and existing.target == p.target:
                return existing
        return proposals.create(
            self.db,
            verdict_id=verdict.verdict_id,
            source="copilot",
            action=p.action,
            target=p.target,
            rationale=p.rationale,
            reversible=p.reversible,
            live_capable=p.live_capable,
        )

    def confirm(self, turn: Turn, *, now: datetime | None = None) -> Opened:
        """The operator said yes: investigate the verdict if nobody has, then open approvals."""
        moment = now or datetime.now(UTC)
        approval_ids: list[str] = []
        proposal_ids: list[str] = []
        investigation_ids: list[str] = []
        with steps.step(
            "copilot.confirm",
            layer="copilot",
            tenant=self.db.tenant,
            inputs={
                "thread_id": self.thread_id,
                "answer_id": turn.answer.answer_id,
                "actions": [p.action for p in turn.pending],
            },
        ) as s:
            for p in turn.pending:
                verdict = verdicts.load(self.db, p.verdict_id)
                if verdict is None:
                    s.event(f"action_rejected:unknown_verdict:{p.action}")
                    continue
                existing = investigations.for_verdict(self.db, p.verdict_id)
                if existing is None:
                    existing = investigator.investigate(
                        self.db, self.settings, verdict, self.providers, now=moment
                    )
                    investigation_ids.append(existing.investigation_id)
                row = self._proposal_row(verdict, p)
                proposal_ids.append(row.proposal_id)
                item = approvals.for_ref(self.db, "remediation", row.proposal_id)
                if item is None:
                    approval_ids.append(
                        approvals.open_item(
                            self.db,
                            "remediation",
                            row.proposal_id,
                            {
                                "verdict_id": verdict.verdict_id,
                                "action": p.action,
                                "target": p.target,
                                "source": "copilot",
                            },
                        )
                    )
                else:
                    approval_ids.append(item.approval_id)
            s.set_output(
                {
                    "approvals": approval_ids,
                    "proposals": proposal_ids,
                    "investigations": investigation_ids,
                }
            )
        return Opened(tuple(approval_ids), tuple(proposal_ids), tuple(investigation_ids))
