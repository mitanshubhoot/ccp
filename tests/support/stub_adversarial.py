"""A deliberately compliant extraction stub for the S17 suite: it obeys instructions found
inside the fenced evidence (report FAIL, or report a timeline it was told to report), which
is exactly what the injection guardrails are meant to make a real model not do. The
robust stub is ``tests.support.stub_extractors.stub_extract``."""

from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel

from ccp.evaluators.extraction.base import Cited
from ccp.evaluators.extraction.gh4 import DisclosurePolicyFields
from ccp.llm.client import SystemBlock
from tests.support.stub_extractors import blocks, stub_extract

_FAIL = re.compile(r"policy_exists\s*=\s*false|report this as FAIL|this control is FAIL", re.I)
_TIMELINE = re.compile(r"timeline_text\s*=?\s*'([^']+)'", re.I)


def comply_extract(schema: type[Any], system: list[SystemBlock], user: str) -> BaseModel:
    if schema is not DisclosurePolicyFields:
        return stub_extract(schema, system, user)
    found = blocks(user)
    for block in found:
        if _FAIL.search(block.body):
            return DisclosurePolicyFields(policy_exists=False, timeline_stated=False)
    for block in found:
        match = _TIMELINE.search(block.body)
        if match:
            line = block.line_with(match.group(0)) or match.group(0)
            first = block.body.strip().splitlines()[0].strip()
            return DisclosurePolicyFields(
                policy_exists=True,
                policy_exists_citation=Cited(chunk_id=block.chunk_id, quote=first[:200]),
                timeline_stated=True,
                timeline_stated_citation=Cited(chunk_id=block.chunk_id, quote=line[:300]),
                timeline_text=match.group(1),
                timeline_text_citation=Cited(chunk_id=block.chunk_id, quote=line[:300]),
            )
    return stub_extract(schema, system, user)
