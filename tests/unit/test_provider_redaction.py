"""NT3 (A-96): nothing that matches a secret pattern leaves for a third-party API. A canary
of planted secrets goes through the Voyage and OpenAI embedding clients and the Voyage
reranker over a recording transport; every text they sent is checked. Voyage is refused
until the training opt-out is recorded; the local step log carries only redacted values."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from ccp.config import Settings
from ccp.observability import steps
from ccp.retrieval.embeddings import (
    DIMENSION,
    OpenAIEmbeddingClient,
    ProviderTermsNotRecorded,
    VoyageEmbeddingClient,
    build_embedding_client,
)
from ccp.retrieval.rerank import VoyageReranker, build_reranker
from ccp.security.secrets_patterns import PATTERNS, redact, scan

GHP = "ghp_A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"
AWS_SECRET = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
CANARY = [
    f"deploy.sh exports GITHUB_TOKEN={GHP} before the release",
    f"aws_secret_access_key = {AWS_SECRET}",
    "keys: AKIAIOSFODNN7EXAMPLE and ASIAIOSFODNN7EXAMPLE",
    "-----BEGIN RSA PRIVATE KEY-----\nMIIEowIBAAKCAQEAu1SU1LfVLPHCozMxH2Mo\n"
    "-----END RSA PRIVATE KEY-----",
    "ANTHROPIC_API_KEY=sk-ant-api03-abcdefghijklmnopqrstuvwxyz012345",
    "The access review covers GitHub and Okta every quarter.",
]


def _recording() -> tuple[httpx.MockTransport, list[dict[str, Any]]]:
    sent: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        sent.append(body)
        if request.url.path.endswith("/embeddings"):
            n = len(body["input"])
            data = [{"index": i, "embedding": [0.0] * DIMENSION} for i in range(n)]
            return httpx.Response(200, json={"data": data, "usage": {"total_tokens": 7}})
        ranked = [{"index": i, "relevance_score": 1.0 / (i + 1)} for i in range(body["top_k"])]
        return httpx.Response(200, json={"data": ranked, "usage": {"total_tokens": 7}})

    return httpx.MockTransport(handler), sent


def _texts(body: dict[str, Any]) -> list[str]:
    out = list(body.get("input") or []) + list(body.get("documents") or [])
    return out + ([body["query"]] if "query" in body else [])


def test_no_secret_reaches_an_embedding_or_rerank_api() -> None:
    transport, sent = _recording()
    VoyageEmbeddingClient("k", transport=transport).embed(CANARY, "document")
    VoyageEmbeddingClient("k", transport=transport).embed([f"where is {GHP} used?"], "query")
    OpenAIEmbeddingClient("k", transport=transport).embed(CANARY, "document")
    VoyageReranker("k", transport=transport).rerank(f"who committed {GHP}?", CANARY, 3)
    texts = [t for body in sent for t in _texts(body)]
    assert len(sent) == 4 and len(texts) == 3 * len(CANARY) + 2
    for text in texts:
        assert scan(text) == [] and redact(text)[1] == [], text
        assert GHP not in text and AWS_SECRET not in text and "MIIEowIBAAK" not in text
    assert "The access review covers GitHub and Okta every quarter." in texts  # clean text kept


def test_voyage_is_refused_until_the_training_opt_out_is_recorded(
    base_settings: Settings,
) -> None:
    keyed = base_settings.model_copy(
        update={"voyage_api_key": SecretStr("k"), "ccp_embed_provider": "voyage"}
    )
    with pytest.raises(ProviderTermsNotRecorded):
        build_embedding_client(keyed)
    with pytest.raises(ProviderTermsNotRecorded):
        build_reranker(keyed.model_copy(update={"retrieval_rerank": True}))
    opted = keyed.model_copy(update={"voyage_training_opt_out": True})
    assert isinstance(build_embedding_client(opted), VoyageEmbeddingClient)
    assert isinstance(
        build_reranker(opted.model_copy(update={"retrieval_rerank": True})), VoyageReranker
    )


@pytest.mark.parametrize(
    ("text", "pattern"),
    [
        (f"aws_secret_access_key = {AWS_SECRET}", "aws_secret_access_key"),
        (f'"SecretAccessKey": "{AWS_SECRET}"', "aws_secret_access_key"),
        ("AWS_SESSION_TOKEN=" + "A" * 120, "aws_session_token"),
        ("key ASIAIOSFODNN7EXAMPLE", "aws_temporary_access_key_id"),
        ("token gho_" + "a1B2" * 9, "github_app_or_oauth_token"),
        ("sk-ant-api03-abcdefghijklmnopqrstuvwxyz012345", "anthropic_api_key"),
        ("OPENAI=sk-proj-abc123def456ghi789jkl012mno", "openai_api_key"),
        ("STRIPE=rk_live_" + "a1B2c3D4" * 3, "stripe_live_key"),
        ("maps AIza" + "SyA1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q", "google_api_key"),
        (
            "https://hooks.slack.com/services/T0123ABCD/B0456EFGH/abcdefghijklmnopqrstuvwx",
            "slack_webhook_url",
        ),
        ("-----BEGIN OPENSSH PRIVATE KEY-----", "private_key_header"),
    ],
)
def test_each_added_format_is_found(text: str, pattern: str) -> None:
    assert pattern in PATTERNS
    assert [h.pattern for h in scan(text)] == [pattern]
    out, names = redact(text)
    assert names and scan(out) == []  # a lone key header goes with the block pattern


def test_a_bare_value_needs_its_name_to_be_found() -> None:
    """A 40-character AWS secret alone looks like any base64 string; it is found by the name
    it is assigned to. This is a limit of pattern matching, recorded in A-96."""
    assert scan(f"rotated {AWS_SECRET} today") == []
    assert scan(f"AWS_SECRET_ACCESS_KEY={AWS_SECRET}") != []


def test_a_whole_private_key_is_redacted_not_only_its_header() -> None:
    body = "MIIEowIBAAKCAQEAu1SU1LfVLPHCozMxH2Mo"
    out, names = redact(
        f"key:\n-----BEGIN RSA PRIVATE KEY-----\n{body}\n-----END RSA PRIVATE KEY-----\nok"
    )
    assert body not in out and out.endswith("ok") and names == ["private_key_block"]
    truncated, _ = redact(f"-----BEGIN EC PRIVATE KEY-----\n{body}\n")
    assert body not in truncated


def test_ordinary_words_are_not_secrets() -> None:
    for text in (
        "risk-assessment-procedures-and-controls",
        "sk-learn-compatible-transformers-for-text",
        "secret_access_key is rotated every 90 days by the platform team",
    ):
        assert scan(text) == [], text


def test_the_local_step_log_keeps_only_redacted_values(
    base_settings: Settings, tmp_path: Path
) -> None:
    settings = base_settings.model_copy(update={"ccp_data_dir": tmp_path})
    steps.configure(settings, sink=None)
    try:
        with (
            pytest.raises(RuntimeError),
            steps.step("test.secret", layer="test", inputs={"text": CANARY[0]}) as s,
        ):
            s.set_output({"note": f"saw {CANARY[1]}", "keys": [CANARY[2]]})
            raise RuntimeError(f"failed while holding {GHP}")
        lines = list(steps.read_lines())
    finally:
        steps.configure(base_settings, sink=None)
    raw = json.dumps(lines)
    assert GHP not in raw and AWS_SECRET not in raw and "AKIAIOSFODNN7EXAMPLE" not in raw
    assert "[REDACTED:github_pat_classic]" in raw
