"""The Copilot streams the answer's prose token by token and records first-token latency."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime

import pytest

from ccp.agent import tier3 as _tier3  # noqa: F401 - registers tier 3
from ccp.compliance_state import writer as state_writer
from ccp.config import Settings
from ccp.copilot import runner, schema
from ccp.evaluators import runner as evaluations
from ccp.evaluators.tier2 import Providers
from ccp.retrieval import index
from ccp.storage.scoped import ScopedDB
from tests.component.test_copilot_stub import make_providers
from tests.support.pipeline import prepared_tenants

pytestmark = pytest.mark.db
NOW = datetime(2026, 9, 11, 12, tzinfo=UTC)


@pytest.fixture(scope="module")
def world(base_settings: Settings) -> Iterator[tuple[Settings, ScopedDB, Providers]]:
    with prepared_tenants(base_settings, "ccp_stream", ("acme",)) as (settings, dbs):
        acme = dbs["acme"]
        providers = make_providers()
        index.refresh(acme, settings, providers.embedder, contextual_enrichment=False)
        evaluations.enqueue_all(acme, settings, now=NOW, tests=("HR-1",))
        evaluations.drain(acme, settings, now=NOW, providers=providers)
        state_writer.drain(acme)
        yield settings, acme, providers


def test_tokens_arrive_before_the_answer_and_first_token_is_recorded(world) -> None:  # type: ignore[no-untyped-def]
    settings, acme, providers = world
    tokens: list[str] = []
    with runner.Session(acme, settings, providers) as session:
        turn = session.ask("Why did HR-1 fail for Jane Smith?", now=NOW, on_token=tokens.append)
    prose = "".join(tokens)
    assert len(tokens) >= 2 and prose  # the stub streams the JSON in several deltas
    assert turn.answer.text.startswith(prose)
    assert turn.answer.first_token_ms is not None and turn.answer.first_token_ms >= 0
    stored = schema.get(acme, turn.answer.answer_id)
    assert stored is not None and stored.first_token_ms == turn.answer.first_token_ms


def test_status_answers_are_not_streamed(world) -> None:  # type: ignore[no-untyped-def]
    settings, acme, providers = world
    tokens: list[str] = []
    with runner.Session(acme, settings, providers) as session:
        turn = session.ask("Are we SOC 2 ready?", now=NOW, on_token=tokens.append)
    assert tokens == []  # the contract may drop sentences, so nothing leaves before the check
    assert "ready" not in turn.answer.text.lower()
