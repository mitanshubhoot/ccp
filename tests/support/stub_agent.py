"""Test-only stand-ins for the tier 3 model calls: extraction of the two HR-4 schemas from
fenced evidence, and a scripted gather fallback that searches once and then reports."""

from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel

from ccp.evaluators.extraction.base import Cited
from ccp.evaluators.extraction.hr4 import AccessReviewFields, CriticalSystemsFields, NamedSystem
from ccp.llm.client import SystemBlock, ToolCall
from tests.support.stub_extractors import blocks, stub_extract

_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
_PDF_ROW = re.compile(r"^(?P<system>\S+)\s+[A-Z][a-z]+ [A-Z][a-z]+\s+\d{4}-\d{2}-\d{2}")
_SKIP_PREFIXES = ("review date", "prepared", "approved", "system", "|---", "---")


def _cells(line: str) -> list[str]:
    return [c.strip() for c in line.strip().strip("|").split("|")]


def extract_critical(user: str) -> CriticalSystemsFields:
    systems: list[NamedSystem] = []
    for block in blocks(user):
        for line in block.body.splitlines():
            if "|" not in line:
                continue
            cells = _cells(line)
            if len(cells) >= 2 and "critical" in cells[1].lower() and cells[0].lower() != "system":
                systems.append(
                    NamedSystem(
                        name=cells[0],
                        citation=Cited(chunk_id=block.chunk_id, quote=line.strip()[:300]),
                    )
                )
    return CriticalSystemsFields(systems=systems)


def extract_scope(user: str) -> AccessReviewFields:
    in_scope: list[NamedSystem] = []
    review_date: str | None = None
    date_citation: Cited | None = None
    for block in blocks(user):
        for line in block.body.splitlines():
            stripped = line.strip()
            lowered = stripped.lower()
            if review_date is None and lowered.startswith(("review date", "prepared", "approved")):
                match = _DATE.search(stripped)
                if match:
                    review_date = match.group(0)
                    date_citation = Cited(chunk_id=block.chunk_id, quote=stripped[:300])
                continue
            if not _DATE.search(stripped) or lowered.startswith(_SKIP_PREFIXES):
                continue
            if "|" in stripped:
                name = _cells(stripped)[0]
            else:  # a PDF-extracted table row: system, a two-word owner, a date
                row = _PDF_ROW.match(stripped)
                if row is None:
                    continue
                name = row.group("system")
            if name.lower() in ("system", "") or not name[0].isalpha():
                continue
            in_scope.append(
                NamedSystem(
                    name=name, citation=Cited(chunk_id=block.chunk_id, quote=stripped[:300])
                )
            )
    return AccessReviewFields(
        review_date=review_date, review_date_citation=date_citation, systems_in_scope=in_scope
    )


def stub_extract_agent(schema: type[Any], system: list[SystemBlock], user: str) -> BaseModel:
    if schema is CriticalSystemsFields:
        return extract_critical(user)
    if schema is AccessReviewFields:
        return extract_scope(user)
    return stub_extract(schema, system, user)


def stub_chat_search_once(
    system: list[SystemBlock], messages: list[dict[str, Any]], tools: list[dict[str, Any]]
) -> tuple[str, list[ToolCall]]:
    """First turn: one search for the missing type; afterwards: a one-sentence report."""
    goal = str(messages[0].get("content", ""))
    if len(messages) == 1:
        target = (
            "critical_systems_list" if "critical_systems_list" in goal else "access_review_report"
        )
        query = (
            "critical systems inventory"
            if target == "critical_systems_list"
            else "user access review scope"
        )
        return "", [
            ToolCall("call-1", "search_evidence", {"query": query, "evidence_type": target})
        ]
    return "Nothing further was found.", []


def stub_chat_off_list_tool(
    system: list[SystemBlock], messages: list[dict[str, Any]], tools: list[dict[str, Any]]
) -> tuple[str, list[ToolCall]]:
    """A model that hallucinates a destructive tool on its first turn."""
    if len(messages) == 1:
        return "", [ToolCall("call-x", "delete_repo", {"repo": "platform"})]
    return "Done.", []
