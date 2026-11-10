"""NT3 (A-96): GH-5 passes only when every listed file was read. The mapper marks files the
connector did not inline and entries it did not list; GH-5 turns either into needs_review,
while a secret in what was read still fails."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from ccp.config import Settings
from ccp.connectors.base import SourceEvent
from ccp.evaluators.context import EvaluationContext
from ccp.evaluators.deterministic import gh5
from ccp.knowledge import graph as knowledge
from ccp.normalize.entities import Evidence
from ccp.normalize.github_mapper import NOT_INLINED, asset_id, map_github_event
from ccp.tenancy import TenantContext
from ccp.types import VerdictStatus

NOW = datetime(2026, 9, 11, 12, tzinfo=UTC)
NODE = "R_kgDOtest01"
GHP = "ghp_A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"


def _listing(
    files: list[dict[str, Any]], not_listed: list[dict[str, Any]] | None = None
) -> Evidence:
    payload: dict[str, Any] = {
        "org": "acme-example",
        "repo": "platform",
        "repo_node_id": NODE,
        "updated_at": "2026-09-01T00:00:00Z",
        "files": files,
    }
    if not_listed is not None:
        payload["not_listed"] = not_listed
    result = map_github_event(
        SourceEvent(
            tenant_id="acme",
            connector="github",
            source_event_id=f"{NODE}:files@1",
            source_ts=datetime(2026, 9, 1, tzinfo=UTC),
            event_type="repo.file_listing.snapshot",
            payload=payload,
            schema_version="github.v1",
        )
    )
    (evidence,) = result.entities
    assert isinstance(evidence, Evidence)
    return evidence


def _verdict(settings: Settings, evidence: Evidence):  # type: ignore[no-untyped-def]
    ctx = EvaluationContext(
        tenant=TenantContext.for_id(settings, "acme"),
        settings=settings,
        spec=knowledge.load().tests["GH-5"],
        trigger_observed_at=NOW,
        now=NOW,
        persons={},
        grants=[],
        assets={},
        evidence=[evidence],
        canonical={},
        pending_review=set(),
    )
    return gh5.evaluate(ctx, f"repo:{asset_id(NODE)}")


def test_every_file_read_and_clean_passes(base_settings: Settings) -> None:
    evidence = _listing([{"path": "README.md", "sha": "a", "size": 9, "content": "# hello\n"}])
    assert evidence.guardrail_events == ()
    v = _verdict(base_settings, evidence)
    assert v.status is VerdictStatus.passed and "every listed file was read" in v.reasoning


def test_a_file_not_read_is_needs_review(base_settings: Settings) -> None:
    evidence = _listing(
        [
            {"path": "README.md", "sha": "a", "size": 9, "content": "# hello\n"},
            {"path": "dump.sql", "sha": "b", "size": 90_000},  # too large to inline
        ]
    )
    assert evidence.guardrail_events == ("files_not_read:1",) and NOT_INLINED in evidence.text
    v = _verdict(base_settings, evidence)
    assert v.status is VerdictStatus.needs_review
    assert "incomplete_file_read" in v.guardrail_events and "files_not_read:1" in v.reasoning


def test_a_directory_not_listed_is_needs_review(base_settings: Settings) -> None:
    evidence = _listing(
        [{"path": "README.md", "sha": "a", "size": 9, "content": "# hello\n"}],
        not_listed=[{"path": "deploy", "type": "dir"}, {"path": "vendor/lib", "type": "submodule"}],
    )
    assert evidence.guardrail_events == ("entries_not_listed:2",)
    assert "### Not listed\n- deploy\n- vendor/lib" in evidence.text
    assert _verdict(base_settings, evidence).status is VerdictStatus.needs_review


def test_a_secret_in_what_was_read_still_fails(base_settings: Settings) -> None:
    evidence = _listing(
        [
            {"path": ".env", "sha": "a", "size": 60, "content": f"GITHUB_TOKEN={GHP}\n"},
            {"path": "dump.sql", "sha": "b", "size": 90_000},
        ]
    )
    v = _verdict(base_settings, evidence)
    assert v.status is VerdictStatus.failed and GHP not in v.reasoning


def test_rows_written_before_the_events_existed_are_read_from_the_marker(
    base_settings: Settings,
) -> None:
    older = _listing([{"path": "big.bin", "sha": "b", "size": 90_000}]).model_copy(
        update={"guardrail_events": ()}
    )
    assert _verdict(base_settings, older).status is VerdictStatus.needs_review
