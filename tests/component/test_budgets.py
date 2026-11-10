"""S18 budgets (spec.md Layer 13): a stub loop past each cap stops with budget_exceeded; a
tenant over its daily budget parks records and resumes after reset; ten changes to one
subject enqueue one evaluation; concurrency never exceeds the cap under a 50-record burst."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import BaseModel

from ccp.config import Settings
from ccp.evaluators import budget_gate, runner
from ccp.evaluators.impact import EvaluationRequest, enqueue
from ccp.ingest import event_log
from ccp.ingest.event_log import LogRecord
from ccp.llm.budget import BudgetTracker, RunBudget
from ccp.llm.client import StubLLMClient, SystemBlock
from ccp.llm.errors import BudgetExceeded
from ccp.storage.scoped import ScopedDB
from tests.support.pipeline import prepared_tenants

NOW = datetime(2026, 9, 11, 12, tzinfo=UTC)


class _Answer(BaseModel):
    ok: bool = True


def _stub() -> StubLLMClient:
    return StubLLMClient(handler=lambda schema, system, user: _Answer())


def _loop(tracker: BudgetTracker, client: StubLLMClient, limit: int = 1000) -> int:
    calls = 0
    for _ in range(limit):
        client.extract(
            schema=_Answer, system=[SystemBlock("s")], user="u", model="m", tracker=tracker
        )
        calls += 1
    return calls


def test_token_cap_stops_the_loop() -> None:
    tracker = BudgetTracker(RunBudget(max_tokens=2_000, max_usd=100, max_seconds=100))
    with pytest.raises(BudgetExceeded, match="budget_exceeded: token cap"):
        _loop(tracker, _stub())
    assert tracker.tokens == 2_400  # 4 calls of 600 tokens; the 4th tripped the cap


def test_usd_cap_stops_the_loop() -> None:
    tracker = BudgetTracker(RunBudget(max_tokens=10**9, max_usd=0.0025, max_seconds=100))
    with pytest.raises(BudgetExceeded, match="budget_exceeded: usd cap"):
        _loop(tracker, _stub())
    assert tracker.tool_calls == 0 and 0.0025 < tracker.usd <= 0.0031


def test_tool_call_cap_stops_the_loop() -> None:
    tracker = BudgetTracker(
        RunBudget(max_tokens=10**9, max_usd=100, max_seconds=100, max_tool_calls=3)
    )
    for _ in range(3):
        tracker.tool_call()
    with pytest.raises(BudgetExceeded, match="budget_exceeded: tool-call cap"):
        tracker.tool_call()


def test_wall_clock_cap_uses_the_injected_clock() -> None:
    ticks = iter([0.0, 1.0, 2.0, 31.0])
    tracker = BudgetTracker(
        RunBudget(max_tokens=10**9, max_usd=100, max_seconds=30), clock=lambda: next(ticks)
    )
    tracker.check_clock()
    tracker.check_clock()
    with pytest.raises(BudgetExceeded, match="budget_exceeded: wall-clock cap"):
        tracker.check_clock()


def test_a_graph_can_lower_a_cap_but_never_raise_it() -> None:
    base = RunBudget(max_tokens=1000, max_usd=1.0, max_seconds=60, max_tool_calls=8)
    lowered = base.lowered(max_tokens=5000, max_usd=0.1, max_tool_calls=2)
    assert lowered == RunBudget(max_tokens=1000, max_usd=0.1, max_seconds=60, max_tool_calls=2)


def test_budget_from_settings_reads_the_three_caps() -> None:
    settings = Settings(
        ccp_run_budget_tokens=123, ccp_run_budget_usd=0.25, ccp_run_budget_seconds=7
    )
    assert RunBudget.from_settings(settings) == RunBudget(
        max_tokens=123, max_usd=0.25, max_seconds=7.0
    )


# -- the tenant daily budget and the concurrency cap need a database ------------------------


@pytest.fixture(scope="module")
def world(base_settings: Settings) -> Iterator[tuple[Settings, dict[str, ScopedDB]]]:
    with prepared_tenants(base_settings, "ccp_budget", ("acme",)) as (settings, dbs):
        yield settings, dbs


@pytest.mark.db
def test_tenant_over_daily_budget_parks_ai_records_and_resumes_after_reset(
    world: tuple[Settings, dict[str, ScopedDB]],
) -> None:
    """The model budget parks only records that would call a model (A-97): a deterministic
    test runs the same day, an interpretive one waits for the reset."""
    settings, dbs = world
    acme = dbs["acme"]
    cap = acme.tenant.settings.daily_budget_usd
    budget_gate.record_spend(acme, cap, tokens=1, recorded_at=NOW - timedelta(hours=1))
    assert budget_gate.daily_spend(acme, NOW) == pytest.approx(cap)
    with acme.transaction() as cur:
        deterministic = event_log.append(
            cur,
            tenant_id="acme",
            topic="evaluations",
            partition_key="acme:HR-5|tenant",
            source_ts=NOW,
            ref=EvaluationRequest("HR-5", "tenant", NOW).to_ref(),
        )
        interpretive = event_log.append(
            cur,
            tenant_id="acme",
            topic="evaluations",
            partition_key="acme:GH-4|tenant",
            source_ts=NOW,
            ref=EvaluationRequest("GH-4", "tenant", NOW).to_ref(),
        )
    consumer = runner.EvaluationsConsumer(acme, settings, now=NOW)
    while consumer.run_once():
        pass
    with acme.transaction() as cur:
        assert event_log.status_of(cur, deterministic)[0] == "done"
        status, attempts, error = event_log.status_of(cur, interpretive)
    assert status == "parked" and attempts == 0 and "budget_exceeded" in (error or "")
    assert consumer.summary.evaluated == 1 and consumer.summary.by_status == {"fail": 1}

    tomorrow = NOW + timedelta(days=1)
    assert budget_gate.daily_spend(acme, tomorrow) == 0.0
    assert runner.requeue_parked(acme) == 1
    runner.drain(acme, settings, now=tomorrow)
    with acme.transaction() as cur:
        status, _, error = event_log.status_of(cur, interpretive)
    # the budget no longer holds it; this keyless run parks it for the missing model key
    assert status == "parked" and "budget_exceeded" not in (error or "")


@pytest.mark.db
def test_spend_ledger_is_append_only_and_tenant_scoped(
    world: tuple[Settings, dict[str, ScopedDB]],
) -> None:
    import psycopg

    acme = world[1]["acme"]
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        acme.execute("DELETE FROM spend_ledger")
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        acme.execute("UPDATE spend_ledger SET usd = 0")


class _Observing(runner.EvaluationsConsumer):
    """Records how many records were claimed at once while each one was being handled."""

    def __init__(self, db: ScopedDB, settings: Settings) -> None:
        # the day after the budget test above exhausted today's cap
        super().__init__(db, settings, now=NOW + timedelta(days=1))
        self.observed: list[int] = []

    def handle(self, record: LogRecord) -> None:
        self.observed.append(self.in_flight())
        super().handle(record)


@pytest.mark.db
def test_concurrency_never_exceeds_the_tenant_cap_under_a_burst(
    world: tuple[Settings, dict[str, ScopedDB]],
) -> None:
    settings, dbs = world
    acme = dbs["acme"]
    cap = acme.tenant.settings.max_concurrency
    assert cap == 4
    with acme.transaction() as cur:
        for i in range(50):  # 50 distinct triggers: no coalescing, no duplicate keys
            enqueue(
                cur,
                "acme",
                EvaluationRequest("HR-5", "tenant", NOW + timedelta(seconds=i + 1)),
                coalesce_seconds=0,
            )
    consumer = _Observing(acme, settings)
    processed = 0
    while True:
        n = consumer.run_once(limit=50)
        if n == 0:
            break
        assert n <= cap
        processed += n
    assert processed == 50 and consumer.summary.evaluated == 50
    assert max(consumer.observed) <= cap and len(consumer.observed) == 50
