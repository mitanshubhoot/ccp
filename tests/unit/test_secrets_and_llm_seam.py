"""Secret patterns (GH-5, S6) and the model seam's error taxonomy and stub client."""

from __future__ import annotations

import pytest
from pydantic import BaseModel

from ccp.config import Settings
from ccp.ingest.consumers import RetryableError, TerminalError
from ccp.llm import client as llm_client
from ccp.llm.budget import BudgetTracker, RunBudget
from ccp.llm.client import SystemBlock
from ccp.llm.errors import (
    BudgetExceeded,
    RetryableModelError,
    TerminalModelError,
    classify_status,
)
from ccp.security.secrets_patterns import PATTERNS, redact, scan

GHP = "ghp_A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"


@pytest.mark.parametrize(
    ("text", "pattern"),
    [
        (f"GITHUB_TOKEN={GHP}", "github_pat_classic"),
        ("github_pat_11ABCDEFG0123456789abcdefghijklmnopqrstuvwxyz", "github_pat_fine_grained"),
        ("aws_access_key_id = AKIAIOSFODNN7EXAMPLE", "aws_access_key_id"),
        ("SLACK=xoxb-123456789012-abcdefghijkl", "slack_token"),
        ("-----BEGIN RSA PRIVATE KEY-----", "private_key_header"),
        ("api_key: abcdefghijklmnopqrstuvwxyz0123", "generic_api_key"),
    ],
)
def test_each_pattern_matches_its_example(text: str, pattern: str) -> None:
    hits = scan(text)
    assert [h.pattern for h in hits] == [pattern]
    assert hits[0].line == 1 and len(hits[0].preview) == 9 and hits[0].preview.endswith("...")
    assert hits[0].preview.isascii()  # safe for any console or log sink


def test_scan_reports_line_numbers_and_ignores_clean_text() -> None:
    text = "README\n\nsecond line is fine\nTOKEN=" + GHP + "\n"
    hits = scan(text)
    assert [(h.pattern, h.line) for h in hits] == [("github_pat_classic", 4)]
    assert scan("nothing to see here; api_key is documented in the wiki") == []


def test_redact_replaces_every_match_and_names_the_patterns() -> None:
    out, names = redact(f"a={GHP} b=AKIAIOSFODNN7EXAMPLE c={GHP}")
    assert GHP not in out and "AKIAIOSFODNN7EXAMPLE" not in out
    assert out.count("[REDACTED:github_pat_classic]") == 2
    assert names == ["aws_access_key_id", "github_pat_classic"]
    assert set(names) <= set(PATTERNS)


def test_error_taxonomy_maps_onto_the_queue_semantics() -> None:
    assert issubclass(RetryableModelError, RetryableError)
    assert issubclass(TerminalModelError, TerminalError)
    assert issubclass(BudgetExceeded, TerminalModelError)
    assert isinstance(classify_status(429), RetryableModelError)
    assert isinstance(classify_status(529), RetryableModelError)
    assert isinstance(classify_status(503), RetryableModelError)
    assert isinstance(classify_status(400, "bad request"), TerminalModelError)
    assert isinstance(classify_status(401), TerminalModelError)
    assert isinstance(classify_status(404), TerminalModelError)


class _Fields(BaseModel):
    answer: str


def test_stub_client_charges_the_tracker_and_records_the_request() -> None:
    stub = llm_client.StubLLMClient(handler=lambda schema, system, user: _Fields(answer="x"))
    tracker = BudgetTracker(RunBudget(max_tokens=10_000, max_usd=1, max_seconds=10))
    result = stub.extract(
        schema=_Fields,
        system=[SystemBlock("sys", cache=True)],
        user="usr",
        model="claude-haiku-4-5",
        tracker=tracker,
    )
    assert isinstance(result.parsed, _Fields) and result.parsed.answer == "x"
    assert result.model == "stub:claude-haiku-4-5"
    assert tracker.tokens == 600 and tracker.usd == pytest.approx(0.001)
    assert stub.calls == [
        {
            "model": "claude-haiku-4-5",
            "system": [{"text": "sys", "cache": True}],
            "documents": [],
            "user": "usr",
            "schema": "_Fields",
            "max_tokens": 2048,
            "reasoning": False,
        }
    ]


def test_keyless_settings_never_yield_a_live_client() -> None:
    settings = Settings(anthropic_api_key=None)
    with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY is not set"):
        llm_client.build_client(settings)
    stub = llm_client.StubLLMClient()
    assert llm_client.build_client(settings, stub) is stub
