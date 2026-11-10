"""NT4 (A-97): the queue and the evaluation path stay correct through failures.

- An ordered topic retries a failing record with a backoff, holding the records behind it,
  then dead-letters it for that consumer only and moves on; a replay re-runs it.
- A parallel retry waits for its backoff; a burst of throttles spends no attempts and
  dead-letters nothing; a dead parallel record can be requeued; parked records unpark.
- A verdict and its announcement commit together; a stored verdict is never re-evaluated.
- Tier 1 keeps flowing with the model budget spent; an unpriced model is refused.
- A retried tier 3 evaluation continues its own thread.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import psycopg
import pytest
from psycopg_pool import ConnectionPool
from pydantic import BaseModel, SecretStr

from ccp.agent import threads, tier3
from ccp.config import Settings
from ccp.evaluators import budget_gate
from ccp.evaluators import runner as evaluations
from ccp.evaluators import verdict as verdicts
from ccp.evaluators.context import load_context
from ccp.evaluators.impact import EvaluationRequest
from ccp.evaluators.impact import enqueue as enqueue_request
from ccp.ingest import event_log, ops
from ccp.ingest.consumers import (
    OrderedConsumer,
    ParallelConsumer,
    ParkedError,
    RetryableError,
    ThrottledError,
)
from ccp.ingest.event_log import LogRecord
from ccp.knowledge import graph as knowledge
from ccp.llm.anthropic_client import AnthropicClient
from ccp.llm.budget import BudgetTracker, RunBudget
from ccp.llm.client import StubLLMClient, SystemBlock
from ccp.llm.errors import RetryableModelError, ThrottledModelError, UnpricedModel, classify_status
from ccp.observability import cost
from ccp.retrieval import index
from ccp.retrieval.config import RetrievalConfig
from ccp.retrieval.embeddings import HashingEmbeddingClient
from ccp.storage.db import app_pool, scratch_database
from ccp.storage.scoped import ScopedDB
from ccp.tenancy import TenantContext
from tests.support.pipeline import prepared_tenants
from tests.support.stub_agent import stub_chat_search_once, stub_extract_agent

NOW = datetime(2026, 9, 11, 12, tzinfo=UTC)
TRIGGER = datetime(2026, 9, 2, 15, tzinfo=UTC)
JANE = "person:hris:person:E1042"


# -- queue: ordered topics ----------------------------------------------------------------


@pytest.fixture
def acme(db_settings: Settings, pool: ConnectionPool) -> ScopedDB:
    return ScopedDB(pool, TenantContext.for_id(db_settings, "acme"))


@pytest.fixture
def lone(base_settings: Settings) -> Iterator[ScopedDB]:
    """A database of its own: a parallel consumer claims every pending record of its topic,
    so nothing another test left behind may be there."""
    try:
        with scratch_database(base_settings, prefix="ccp_queue") as settings:
            scoped_pool = app_pool(settings, max_size=2)
            try:
                yield ScopedDB(scoped_pool, TenantContext.for_id(settings, "acme"))
            finally:
                scoped_pool.close()
    except psycopg.OperationalError as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"Postgres not reachable: {exc}")


class _Picky(OrderedConsumer):
    """Fails on refs it is told to, records the order it handled the rest in."""

    topic = "raw.hris"

    def __init__(self, db: ScopedDB, name: str, poison: set[str], base_delay: float = 0.0):
        self.name = name
        self.base_delay = base_delay
        self.max_attempts = 3
        super().__init__(db)
        self.poison = poison
        self.seen: list[str] = []

    def handle(self, cur: psycopg.Cursor[Any], record: LogRecord) -> None:
        if record.ref in self.poison:
            raise RetryableError(f"cannot map {record.ref}")
        self.seen.append(record.ref)


def _fresh(consumer: OrderedConsumer, db: ScopedDB) -> None:
    """Start the consumer after everything other tests left on the shared topic."""
    with db.transaction() as cur:
        row = cur.execute('SELECT coalesce(max("offset"), 0) FROM event_log').fetchone()
        assert row is not None
        event_log.advance_cursor(cur, consumer.cursor_name, consumer.topic, int(row[0]))


def _append(db: ScopedDB, topic: str, ts: str, ref: str) -> int:
    with db.transaction() as cur:
        return event_log.append(
            cur,
            tenant_id=db.tenant.tenant_id,
            topic=topic,
            partition_key="p",
            source_ts=datetime.fromisoformat(ts),
            ref=ref,
        )


@pytest.mark.db
def test_a_poison_record_dead_letters_and_the_topic_moves_on(acme: ScopedDB) -> None:
    consumer = _Picky(acme, "test-picky-1", poison={"bad"})
    _fresh(consumer, acme)
    _append(acme, "raw.hris", "2026-08-01T00:00:00+00:00", "a")
    bad = _append(acme, "raw.hris", "2026-08-02T00:00:00+00:00", "bad")
    _append(acme, "raw.hris", "2026-08-03T00:00:00+00:00", "c")
    assert consumer.run_once() == 1 and consumer.seen == ["a"]  # stops at the failure
    assert consumer.run_once() == 0  # second try fails, still holds "c"
    assert consumer.run_once() == 2  # third try: dead-lettered, then "c"
    assert consumer.seen == ["a", "c"]
    dead = ops.dead_letters(acme)
    assert [(d.offset, d.consumer, d.attempts) for d in dead if d.offset == bad] == [
        (bad, "test-picky-1", 3)
    ]
    assert consumer.run_once() == 0  # nothing left; the dead record is not retried
    consumer.poison.clear()  # the mapping is fixed: replay the dead record
    assert consumer.replay(bad, "ops") == "done" and consumer.seen == ["a", "c", "bad"]
    assert all(d.offset != bad for d in ops.dead_letters(acme))
    with acme.transaction() as cur:
        replays = cur.execute(
            'SELECT outcome, replayed_by FROM dead_letter_replays WHERE "offset" = %s', (bad,)
        ).fetchall()
    assert replays == [("done", "ops")]


@pytest.mark.db
def test_a_backing_off_record_holds_the_records_behind_it(acme: ScopedDB) -> None:
    consumer = _Picky(acme, "test-picky-2", poison={"slow"}, base_delay=3600.0)
    _fresh(consumer, acme)
    _append(acme, "raw.hris", "2026-08-01T00:00:00+00:00", "slow")
    _append(acme, "raw.hris", "2026-08-02T00:00:00+00:00", "after")
    assert consumer.run_once() == 0  # first try fails; an hour's wait begins
    consumer.poison.clear()
    assert consumer.run_once() == 0 and consumer.seen == []  # still waiting: order holds


@pytest.mark.db
def test_one_consumers_dead_letter_does_not_hide_the_record_from_another(
    acme: ScopedDB,
) -> None:
    strict = _Picky(acme, "test-strict", poison={"odd"})
    lenient = _Picky(acme, "test-lenient", poison=set())
    _fresh(strict, acme)
    _fresh(lenient, acme)
    _append(acme, "raw.hris", "2026-08-01T00:00:00+00:00", "odd")
    for _ in range(3):
        strict.run_once()
    assert lenient.run_once() == 1 and lenient.seen == ["odd"]
    assert strict.seen == []


# -- queue: parallel topics ---------------------------------------------------------------


class _Scripted(ParallelConsumer):
    name = "test-scripted"
    topic = "evaluations"

    def __init__(self, db: ScopedDB, errors: list[Exception], base_delay: float = 0.0):
        super().__init__(db, max_attempts=3, visibility_seconds=300, base_delay=base_delay)
        self.errors = errors
        self.calls = 0

    def handle(self, record: LogRecord) -> None:
        self.calls += 1
        if self.errors:
            raise self.errors.pop(0)


def _state(db: ScopedDB, offset: int) -> tuple[str, int, int]:
    with db.transaction() as cur:
        row = cur.execute(
            'SELECT status, attempts, deferrals FROM event_log WHERE "offset" = %s', (offset,)
        ).fetchone()
    assert row is not None
    return str(row[0]), int(row[1]), int(row[2])


@pytest.mark.db
def test_a_parallel_retry_waits_for_its_backoff(lone: ScopedDB) -> None:
    offset = _append(lone, "evaluations", "2026-08-01T00:00:00+00:00", "wait-me")
    consumer = _Scripted(lone, [RetryableError("flaky")], base_delay=3600.0)
    while consumer.run_once(limit=100):
        pass
    assert consumer.calls == 1 and _state(lone, offset)[:2] == ("pending", 1)
    assert consumer.run_once(limit=100) == 0  # not claimable for an hour


@pytest.mark.db
def test_a_burst_of_throttles_spends_no_attempts_and_dead_letters_nothing(
    lone: ScopedDB,
) -> None:
    offset = _append(lone, "evaluations", "2026-08-01T00:00:00+00:00", "throttled")
    burst: list[Exception] = [ThrottledModelError(f"HTTP 429: slow down {n}") for n in range(8)]
    consumer = _Scripted(lone, burst)
    while consumer.run_once(limit=100):
        pass
    status, attempts, deferrals = _state(lone, offset)
    assert consumer.calls == 9 and status == "done" and deferrals == 8 and attempts == 1
    with lone.transaction() as cur:
        assert cur.execute(
            'SELECT count(*) FROM dead_letter WHERE "offset" = %s', (offset,)
        ).fetchone() == (0,)
    assert isinstance(classify_status(429), ThrottledError)
    assert isinstance(classify_status(529), ThrottledModelError)
    assert not isinstance(classify_status(500), ThrottledError)
    assert isinstance(classify_status(500), RetryableModelError)


@pytest.mark.db
def test_a_dead_parallel_record_is_requeued_and_parked_records_unpark(lone: ScopedDB) -> None:
    offset = _append(lone, "evaluations", "2026-08-01T00:00:00+00:00", "dies")
    consumer = _Scripted(lone, [RetryableError(f"no {n}") for n in range(3)])
    while consumer.run_once(limit=100):
        pass
    assert _state(lone, offset)[0] == "dead"
    assert ops.replay(lone, Settings(), offset, "ops") == {"evaluations": "requeued"}
    assert _state(lone, offset)[:2] == ("pending", 0)
    while consumer.run_once(limit=100):
        pass
    assert _state(lone, offset)[0] == "done"
    parked = _append(lone, "evaluations", "2026-08-01T00:00:00+00:00", "parks")

    class _Parks(_Scripted):
        def handle(self, record: LogRecord) -> None:
            raise ParkedError("budget_exceeded: test")

    _Parks(lone, []).run_once(limit=100)
    assert _state(lone, parked)[0] == "parked"
    assert ops.unpark(lone) >= 1 and _state(lone, parked)[0] == "pending"


# -- the evaluation path --------------------------------------------------------------------


@pytest.fixture(scope="module")
def world(base_settings: Settings) -> Iterator[tuple[Settings, dict[str, ScopedDB]]]:
    with prepared_tenants(base_settings, "ccp_reliab") as (settings, dbs):
        yield settings, dbs


def _queue(db: ScopedDB, test_id: str, subject: str, trigger: datetime = TRIGGER) -> None:
    with db.transaction() as cur:
        enqueue_request(
            cur, db.tenant.tenant_id, EvaluationRequest(test_id, subject, trigger, "full_run"), 0
        )


@pytest.mark.db
def test_a_verdict_and_its_announcement_commit_together(
    world: tuple[Settings, dict[str, ScopedDB]], monkeypatch: pytest.MonkeyPatch
) -> None:
    settings, dbs = world
    acme = dbs["acme"]
    request = EvaluationRequest("HR-1", JANE, TRIGGER, "full_run")
    _queue(acme, "HR-1", JANE)
    key = evaluations.expected_key(settings, "acme", request)
    real_append = event_log.append
    crashes = {"left": 1}

    def crash_on_announce(cur: Any, **kwargs: Any) -> int:
        if kwargs.get("topic") == "verdicts" and crashes["left"]:
            crashes["left"] -= 1
            raise RuntimeError("worker killed between verdict and announcement")
        return real_append(cur, **kwargs)

    monkeypatch.setattr(event_log, "append", crash_on_announce)
    no_wait = settings.model_copy(update={"ccp_retry_base_seconds": 0.0})
    consumer = evaluations.EvaluationsConsumer(acme, no_wait, now=NOW)
    assert consumer.run_once() == 1  # evaluated; the commit died at the announcement
    assert verdicts.load_by_key(acme, key) is None  # the crash took the verdict with it
    assert evaluations.unannounced(acme) == 0
    summary = evaluations.drain(acme, no_wait, now=NOW)  # the retry
    assert summary.evaluated == 1 and summary.duplicates == 0
    assert verdicts.load_by_key(acme, key) is not None and evaluations.unannounced(acme) == 0


@pytest.mark.db
def test_a_stored_verdict_is_not_evaluated_again(
    world: tuple[Settings, dict[str, ScopedDB]], monkeypatch: pytest.MonkeyPatch
) -> None:
    settings, dbs = world
    acme = dbs["acme"]
    trigger = datetime(2026, 9, 3, tzinfo=UTC)
    _queue(acme, "HR-1", JANE, trigger)
    first = evaluations.drain(acme, settings, now=NOW)
    assert first.evaluated == 1 and first.duplicates == 0
    calls: list[str] = []
    real = evaluations.evaluate_request

    def counting(*args: Any, **kwargs: Any) -> Any:
        calls.append("called")
        return real(*args, **kwargs)

    monkeypatch.setattr(evaluations, "evaluate_request", counting)
    _queue(acme, "HR-1", JANE, trigger)  # the same request again (a redelivery)
    again = evaluations.drain(acme, settings, now=NOW)
    assert again.evaluated == 1 and again.duplicates == 1 and calls == []


@pytest.mark.db
def test_tier_1_keeps_flowing_when_the_model_budget_is_spent(
    world: tuple[Settings, dict[str, ScopedDB]],
) -> None:
    settings, dbs = world
    globex = dbs["globex"]
    budget_gate.record_spend(globex, 10_000.0, recorded_at=NOW)  # far past any daily cap
    trigger = datetime(2026, 9, 4, tzinfo=UTC)
    _queue(globex, "HR-1", "person:hris:person:G2001", trigger)
    _queue(globex, "HR-4", "tenant", trigger)
    keyed = tier3.Providers(
        embedder=HashingEmbeddingClient(),
        reranker=None,
        llm=StubLLMClient(handler=stub_extract_agent, chat_handler=stub_chat_search_once),
        retrieval=RetrievalConfig(),
    )
    summary = evaluations.drain(globex, settings, now=NOW, providers=keyed)
    assert summary.evaluated >= 1  # tier 1 ran
    with globex.transaction() as cur:
        parked = cur.execute(
            "SELECT ref, last_error FROM event_log WHERE topic = 'evaluations'"
            " AND status = 'parked'"
        ).fetchall()
    assert parked and all("HR-4" in ref and "budget_exceeded" in err for ref, err in parked)


class _Answer(BaseModel):
    answer: str


def test_an_unpriced_model_is_refused_before_any_call() -> None:
    sent: list[dict[str, Any]] = []

    def parse(**kwargs: Any) -> Any:
        sent.append(kwargs)
        raise AssertionError("the API must not be called")

    client = AnthropicClient(
        Settings(anthropic_api_key=SecretStr("k")),
        client=SimpleNamespace(messages=SimpleNamespace(parse=parse, create=parse)),
        sleep=lambda s: None,
    )
    tracker = BudgetTracker(RunBudget(max_usd=1.0, max_tokens=10_000, max_seconds=60))
    assert not cost.is_priced("claude-mystery-9") and cost.is_priced("claude-haiku-4-5-20251001")
    with pytest.raises(UnpricedModel):
        client.extract(
            schema=_Answer,
            system=[SystemBlock("s")],
            user="u",
            model="claude-mystery-9",
            tracker=tracker,
        )
    with pytest.raises(UnpricedModel):
        client.complete(system=[], user="u", model="claude-mystery-9", tracker=tracker)
    assert sent == [] and tracker.usd == 0


# -- tier 3: a retry continues its thread ---------------------------------------------------


@pytest.mark.db
def test_a_retried_tier_3_evaluation_continues_its_own_thread(
    world: tuple[Settings, dict[str, ScopedDB]],
) -> None:
    settings, dbs = world
    acme = dbs["acme"]
    embedder = HashingEmbeddingClient()
    index.refresh(acme, settings, embedder, contextual_enrichment=False)
    failures = {"left": 3}  # more than the node's own three tries: the run fails

    def flaky_extract(schema: type[Any], system: list[SystemBlock], user: str) -> BaseModel:
        if schema.__name__ == "AccessReviewFields" and failures["left"]:
            failures["left"] -= 1
            raise RetryableModelError("HTTP 500: provider hiccup")
        return stub_extract_agent(schema, system, user)

    providers = tier3.Providers(
        embedder=embedder,
        reranker=None,
        llm=StubLLMClient(handler=flaky_extract, chat_handler=stub_chat_search_once),
        retrieval=RetrievalConfig(),
    )
    spec = knowledge.load().tests["HR-4"]
    trigger = datetime(2026, 9, 5, tzinfo=UTC)
    ctx = load_context(acme, settings, spec, trigger, NOW, reason="full_run")
    with pytest.raises(RetryableModelError):
        tier3.evaluate(acme, settings, ctx, spec, "tenant", providers, reason="full_run")
    failed = [t for t in threads.all_threads(acme) if t.status == "failed"]
    assert len(failed) == 1 and failed[0].trajectory[:2] == [
        "fetch_critical_list",
        "extract_systems",
    ]
    verdict = tier3.evaluate(acme, settings, ctx, spec, "tenant", providers, reason="full_run")
    thread_id = (verdict.request_json or {})["thread_id"]
    assert thread_id == failed[0].thread_id  # the same thread, not a new one
    row = threads.get(acme, thread_id)
    assert row is not None and row.status in ("finished", "interrupted")
    assert row.trajectory.count("fetch_critical_list") == 1  # done steps did not run again
    assert verdicts.load_by_key(acme, verdict.evaluation_key) is None  # nothing stored yet
