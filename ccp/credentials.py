"""The credential module (spec.md A-116): the one place a secret is read.

    purpose              setting                              read by
    model:anthropic      ANTHROPIC_API_KEY                    the Anthropic client
    judge:openai         OPENAI_API_KEY                       the cross-family fixture judge
    embed:openai         OPENAI_API_KEY                       embeddings, OpenAI backend
    embed:voyage         VOYAGE_API_KEY                       embeddings
    rerank:voyage        VOYAGE_API_KEY                       reranking
    trace:langfuse_public / trace:langfuse_secret             tracing
    github:read          GITHUB_TOKEN                         the GitHub connector's pulls

GitHub's write credential has no purpose here: only the remediation broker
(ccp/remediation/broker.py) reaches it, through `remediation_token`, after it has checked
that the operation was approved, and it records every credential it issues. No other module
calls `get_secret_value` (a test reads the code for it), and the agent packages may not
import this module or the broker (an import contract): a tool or a model never holds one.
"""

from __future__ import annotations

from typing import Literal

from pydantic import SecretStr

from ccp.config import Settings

Purpose = Literal[
    "model:anthropic",
    "judge:openai",
    "embed:openai",
    "embed:voyage",
    "rerank:voyage",
    "trace:langfuse_public",
    "trace:langfuse_secret",
    "github:read",
]

_SETTING: dict[str, str] = {
    "model:anthropic": "anthropic_api_key",
    "judge:openai": "openai_api_key",
    "embed:openai": "openai_api_key",
    "embed:voyage": "voyage_api_key",
    "rerank:voyage": "voyage_api_key",
    "trace:langfuse_public": "langfuse_public_key",
    "trace:langfuse_secret": "langfuse_secret_key",
    "github:read": "github_token",
}


def _read(value: SecretStr | None) -> str | None:
    return value.get_secret_value() if value is not None else None


def secret(settings: Settings, purpose: Purpose) -> str | None:
    """The secret for one declared purpose, or None when it is not configured."""
    return _read(getattr(settings, _SETTING[purpose]))


def configured(settings: Settings, purpose: Purpose) -> bool:
    return getattr(settings, _SETTING[purpose]) is not None


def remediation_token(settings: Settings) -> str | None:
    """GitHub's write credential, for the remediation broker alone (A-116)."""
    return _read(settings.github_token)
