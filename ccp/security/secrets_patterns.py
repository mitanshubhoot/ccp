"""Plaintext secret detection (GH-5) and redaction before any text reaches a prompt, a log, a
trace, or a third-party API (A-96)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from ccp.observability import tracing

_B64 = r"A-Za-z0-9/+="

# Order matters for redact(): a pattern that covers a whole block runs before one that covers
# only its first line, so the block is removed rather than just its header.
PATTERNS: dict[str, re.Pattern[str]] = {
    "aws_access_key_id": re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    "github_pat_classic": re.compile(r"\bghp_[A-Za-z0-9]{36}\b"),
    "github_pat_fine_grained": re.compile(r"\bgithub_pat_[A-Za-z0-9_]{22,}\b"),
    "slack_token": re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"),
    "private_key_block": re.compile(
        r"-----BEGIN (?:RSA |EC |OPENSSH |DSA |ENCRYPTED |PGP |)PRIVATE KEY(?: BLOCK)?-----"
        r"(?:[\s\S]*?-----END [A-Z ]*PRIVATE KEY(?: BLOCK)?-----|[A-Za-z0-9+/=\s]*)"
    ),
    "private_key_header": re.compile(
        r"-----BEGIN (?:RSA |EC |OPENSSH |DSA |ENCRYPTED |PGP |)PRIVATE KEY(?: BLOCK)?-----"
    ),
    "generic_api_key": re.compile(
        r"(?i)\b(?:api[_-]?key|secret[_-]?key|access[_-]?token)\b\s*[:=]\s*['\"]?[A-Za-z0-9_\-]{20,}"
    ),
    "aws_temporary_access_key_id": re.compile(r"\bASIA[0-9A-Z]{16}\b"),
    "aws_secret_access_key": re.compile(
        rf"(?i)\b(?:aws_?)?secret_?access_?key\b['\"]?\s*[:=]\s*['\"]?[{_B64}]{{40}}(?![{_B64}])"
    ),
    "aws_session_token": re.compile(
        rf"(?i)\b(?:aws_?)?session_?token\b['\"]?\s*[:=]\s*['\"]?[{_B64}]{{100,}}"
    ),
    "github_app_or_oauth_token": re.compile(r"\bgh[ousr]_[A-Za-z0-9]{36}\b"),
    "anthropic_api_key": re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{20,}"),
    "openai_api_key": re.compile(  # a key has digits; a word like sk-learn-... does not
        r"\bsk-(?!ant-)(?:proj-|svcacct-|admin-)?(?=[A-Za-z0-9_\-]*\d)[A-Za-z0-9_\-]{20,}"
    ),
    "stripe_live_key": re.compile(r"\b(?:sk|rk)_live_[0-9A-Za-z]{24,}\b"),
    "google_api_key": re.compile(r"\bAIza[0-9A-Za-z_\-]{35}(?![0-9A-Za-z_\-])"),
    "slack_webhook_url": re.compile(
        r"https://hooks\.slack\.com/services/T[A-Za-z0-9_]+/B[A-Za-z0-9_]+/[A-Za-z0-9_]+"
    ),
}

# scan() reads line by line, so a pattern that spans lines finds nothing there; the header
# pattern reports the key instead.
_MULTILINE = {"private_key_block"}

PREVIEW_CHARS = 6


@dataclass(frozen=True)
class SecretFinding:
    pattern: str
    preview: str  # the first six characters then "..."; never the full value
    line: int


def scan(text: str) -> list[SecretFinding]:
    findings: list[SecretFinding] = []
    for line_no, line in enumerate(text.splitlines(), start=1):
        for name, pattern in PATTERNS.items():
            if name in _MULTILINE:
                continue
            for match in pattern.finditer(line):
                value = match.group(0)
                findings.append(SecretFinding(name, value[:PREVIEW_CHARS] + "...", line_no))
    return findings


def redact_value(value: Any) -> Any:
    """Redact every string inside a JSON-like value (the mask for tracing payloads)."""
    if isinstance(value, str):
        return redact(value)[0]
    if isinstance(value, dict):
        return {k: redact_value(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [redact_value(v) for v in value]
    return value


def redact(text: str) -> tuple[str, list[str]]:
    """Replace every match with [REDACTED:<pattern>]; return the text and the pattern names hit."""
    hits: list[str] = []
    out = text
    for name, pattern in PATTERNS.items():
        if pattern.search(out):
            hits.append(name)
            out = pattern.sub(f"[REDACTED:{name}]", out)
    return out, hits


def redact_all(texts: list[str] | tuple[str, ...]) -> list[str]:
    """What a third-party API may receive: every text with every pattern removed."""
    return [redact(t)[0] for t in texts]


def _mask(*, data: Any, **_: Any) -> Any:
    return redact_value(data)


tracing.register_mask(_mask)  # every trace payload, local or exported, uses the same patterns
