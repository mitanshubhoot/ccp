"""Post-generation citation and support verification (spec.md S3, Layer 7 step 5).

    for each cited field
      chunk id must be one the model was shown          else citation_unknown_chunk
      quote must be a normalized substring of the chunk else citation_quote_not_found
        (token containment >= 0.85 as the fallback)
      the field's value must appear in its quote        else citation_unsupported_value
    a field that fails is set to None; the rule decides with it missing.
A positive claim without any citation is dropped too; absence (False, None, "unknown")
needs no citation.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel

from ccp.evaluators.extraction import SchemaMeta
from ccp.evaluators.verdict import Citation

MAX_QUOTE_CHARS = 300
TOKEN_CONTAINMENT = 0.85
_WS = re.compile(r"\s+")
_TOKEN = re.compile(r"[a-z0-9]+")
ABSENT_VALUES: tuple[Any, ...] = (None, False, "unknown", "not_started", "")


def normalize(text: str) -> str:
    return _WS.sub(" ", text).strip().lower()


def quote_supported(quote: str, chunk_text: str) -> bool:
    q, c = normalize(quote), normalize(chunk_text)
    if not q:
        return False
    if q in c:
        return True
    q_tokens = Counter(_TOKEN.findall(q))
    if not q_tokens:
        return False
    c_tokens = Counter(_TOKEN.findall(c))
    contained = sum(min(n, c_tokens.get(tok, 0)) for tok, n in q_tokens.items())
    return contained / sum(q_tokens.values()) >= TOKEN_CONTAINMENT


def value_in_quote(value: Any, quote: str) -> bool:
    if value in ABSENT_VALUES or isinstance(value, bool):
        return True
    q = normalize(quote)
    v = normalize(str(value))
    if v in q:
        return True
    v_tokens = _TOKEN.findall(v)
    q_tokens = set(_TOKEN.findall(q))
    return bool(v_tokens) and all(tok in q_tokens for tok in v_tokens)


@dataclass
class VerifiedFields:
    values: dict[str, Any]
    citations: list[Citation] = field(default_factory=list)
    events: list[str] = field(default_factory=list)
    required_total: int = 0
    required_covered: int = 0
    citations_given: int = 0
    citations_verified: int = 0

    @property
    def coverage(self) -> float:
        return self.required_covered / self.required_total if self.required_total else 1.0

    @property
    def citation_rate(self) -> float:
        return self.citations_verified / self.citations_given if self.citations_given else 1.0

    @property
    def lost_required_citation(self) -> bool:
        return any(e.split(":")[1] in self.required_names for e in self.events if ":" in e)

    required_names: tuple[str, ...] = ()


def verify_fields(parsed: BaseModel, chunks: dict[str, str], meta: SchemaMeta) -> VerifiedFields:
    values: dict[str, Any] = {}
    out = VerifiedFields(
        values=values, required_total=len(meta.required), required_names=meta.required
    )
    for name in meta.cited:
        value = getattr(parsed, name, None)
        cited = getattr(parsed, f"{name}_citation", None)
        if cited is None:
            if value in ABSENT_VALUES:
                values[name] = value
                if name in meta.required:
                    out.required_covered += 1
            else:
                values[name] = None
                out.events.append(f"uncited_field:{name}")
            continue
        out.citations_given += 1
        chunk_id, quote = str(cited.chunk_id), str(cited.quote)[:MAX_QUOTE_CHARS]
        chunk_text = chunks.get(chunk_id)
        verified = False
        if chunk_text is None:
            out.events.append(f"citation_unknown_chunk:{name}:{chunk_id}")
        elif not quote_supported(quote, chunk_text):
            out.events.append(f"citation_quote_not_found:{name}:{chunk_id}")
        elif name in meta.value_in_quote and not value_in_quote(value, quote):
            out.events.append(f"citation_unsupported_value:{name}:{chunk_id}")
        else:
            verified = True
        out.citations.append(
            Citation(chunk_id=chunk_id, quote=quote, supports=name, verified=verified)
        )
        if verified:
            out.citations_verified += 1
            values[name] = value
            if name in meta.required:
                out.required_covered += 1
        else:
            values[name] = None
    for name, value in parsed.model_dump().items():  # uncited scalar fields pass through
        if name not in values and not name.endswith("_citation"):
            values[name] = value
    return out
