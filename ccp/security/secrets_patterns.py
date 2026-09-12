"""Plaintext secret detection (GH-5) and redaction before any text reaches a prompt or log."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from ccp.observability import tracing

PATTERNS: dict[str, re.Pattern[str]] = {
    "aws_access_key_id": re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    "github_pat_classic": re.compile(r"\bghp_[A-Za-z0-9]{36}\b"),
    "github_pat_fine_grained": re.compile(r"\bgithub_pat_[A-Za-z0-9_]{22,}\b"),
    "slack_token": re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"),
    "private_key_header": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA |)PRIVATE KEY-----"),
    "generic_api_key": re.compile(
        r"(?i)\b(?:api[_-]?key|secret[_-]?key|access[_-]?token)\b\s*[:=]\s*['\"]?[A-Za-z0-9_\-]{20,}"
    ),
}


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


def _mask(*, data: Any, **_: Any) -> Any:
    return redact_value(data)


tracing.register_mask(_mask)  # every trace payload goes through the same patterns
