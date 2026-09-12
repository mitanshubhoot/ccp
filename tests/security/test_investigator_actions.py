"""S12: Investigator proposals are bounded to the action catalogue. A model that proposes an
action outside the closed enum fails schema validation; the investigation is persisted with
no proposed actions and a guardrail event, and no proposal row is written."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from ccp.config import Settings
from ccp.evaluators import runner as evaluations
from ccp.evaluators.tier2 import Providers
from ccp.evaluators.verdict import latest_per_subject
from ccp.investigation import runner, schema
from ccp.investigation.schema import InvestigationDraft
from ccp.llm.client import StubLLMClient
from ccp.remediation import proposals
from ccp.retrieval import index
from ccp.retrieval.config import RetrievalConfig
from ccp.retrieval.embeddings import HashingEmbeddingClient
from ccp.storage.scoped import ScopedDB
from tests.support.pipeline import prepared_tenants
from tests.support.stub_investigator import diagnose_off_catalogue, gather_once

pytestmark = pytest.mark.db
NOW = datetime(2026, 9, 11, 12, tzinfo=UTC)
JANE = "person:hris:person:E1042"


@pytest.fixture(scope="module")
def world(base_settings: Settings) -> Iterator[tuple[Settings, ScopedDB, Providers]]:
    with prepared_tenants(base_settings, "ccp_s12", ("acme",)) as (settings, dbs):
        acme = dbs["acme"]
        providers = Providers(
            embedder=HashingEmbeddingClient(),
            reranker=None,
            llm=StubLLMClient(handler=diagnose_off_catalogue, chat_handler=gather_once),
            retrieval=RetrievalConfig(),
        )
        index.refresh(acme, settings, providers.embedder, contextual_enrichment=False)
        evaluations.enqueue_all(acme, settings, now=NOW, tests=("HR-1",))
        evaluations.drain(acme, settings, now=NOW)
        yield settings, acme, providers


def test_the_schema_itself_rejects_an_off_catalogue_action() -> None:
    with pytest.raises(ValidationError):
        InvestigationDraft.model_validate(
            {
                "root_cause": "x",
                "root_cause_category": "configuration",
                "missing_evidence": [],
                "proposed_actions": [
                    {
                        "action": "delete_repository",
                        "target": {"repo": "acme-example/platform"},
                        "rationale": "no",
                    }
                ],
                "citations": [],
            }
        )


def test_an_off_catalogue_proposal_is_dropped_and_no_proposal_row_is_written(world) -> None:  # type: ignore[no-untyped-def]
    settings, acme, providers = world
    verdict = {v.subject: v for v in latest_per_subject(acme, "HR-1")}[JANE]
    inv = runner.investigate(acme, settings, verdict, providers, now=NOW)
    assert inv.proposed_actions == ()
    assert "action_rejected:unknown_action:delete_repository" in inv.guardrail_events
    assert proposals.for_verdict(acme, verdict.verdict_id) == []
    stored = schema.for_verdict(acme, verdict.verdict_id)
    assert (
        stored is not None
        and stored.proposed_actions == ()
        and stored.root_cause_category == "configuration"
    )
    assert acme.fetch_one("SELECT count(*) FROM approvals WHERE kind = 'remediation'") == (0,)
