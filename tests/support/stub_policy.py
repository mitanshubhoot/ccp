"""Test-only stand-ins for the Policy Agent's model calls."""

from __future__ import annotations

import json
import re
from typing import Any

from pydantic import BaseModel

from ccp.evaluators.extraction.base import Cited
from ccp.evaluators.extraction.policy import GapReportDraft, PolicyGapDraft
from ccp.llm.client import SystemBlock, ToolCall
from tests.support.stub_extractors import blocks

_TIMELINE = re.compile(r"within \d+ (?:business |working )?(?:days|hours)", re.IGNORECASE)
_IDS = re.compile(r"Requirement ids to report on, exactly: (?P<ids>\[.*?\])")


def analyze(schema: type[Any], system: list[SystemBlock], user: str) -> BaseModel:
    """Covered when the fenced policy text states a response timeline (CC7.1) or a coordinated,
    severity-based process (A.8.8); cites the verbatim line it found."""
    match = _IDS.search(user)
    ids = json.loads(match.group("ids")) if match else []
    found = blocks(user)
    gaps: list[PolicyGapDraft] = []
    for req_id in ids:
        citations: list[Cited] = []
        for block in found:
            for line in block.body.splitlines():
                lowered = line.lower()
                if (
                    req_id.endswith("CC7.1")
                    and _TIMELINE.search(line)
                    and "report" in block.body.lower()
                ):
                    citations.append(Cited(chunk_id=block.chunk_id, quote=line.strip()[:300]))
                elif req_id.endswith("A.8.8") and (
                    "coordinated" in lowered or "severity" in lowered
                ):
                    citations.append(Cited(chunk_id=block.chunk_id, quote=line.strip()[:300]))
        if citations:
            gaps.append(
                PolicyGapDraft(requirement_id=req_id, covered=True, citations=citations[:2])
            )
        else:
            what = (
                "no response timeline is stated for reports"
                if req_id.endswith("CC7.1")
                else "no coordinated, severity-based disclosure process is described"
            )
            gaps.append(
                PolicyGapDraft(
                    requirement_id=req_id, covered=False, citations=[], gap_description=what
                )
            )
    covered = [g.requirement_id for g in gaps if g.covered]
    names = ", ".join(covered) or "none"
    return GapReportDraft(
        gaps=gaps, summary=f"{len(covered)} of {len(gaps)} requirements covered: {names}"
    )


def analyze_overclaims(schema: type[Any], system: list[SystemBlock], user: str) -> BaseModel:
    """A model that marks everything covered with quotes that are not in the evidence."""
    match = _IDS.search(user)
    ids = json.loads(match.group("ids")) if match else []
    found = blocks(user)
    chunk_id = found[0].chunk_id if found else "nope"
    return GapReportDraft(
        gaps=[
            PolicyGapDraft(
                requirement_id=r,
                covered=True,
                citations=[Cited(chunk_id=chunk_id, quote="we promise to answer in 1 hour")],
            )
            for r in ids
        ],
        summary="everything is fine",
    )


def gather(
    system: list[SystemBlock], messages: list[dict[str, Any]], tools: list[dict[str, Any]]
) -> tuple[str, list[ToolCall]]:
    """Searches policy documents once, and once tries to read training records (refused)."""
    if len(messages) == 1:
        return "", [
            ToolCall(
                "p-1",
                "search_evidence",
                {
                    "query": "vulnerability disclosure response timeline",
                    "evidence_type": "policy_document",
                },
            ),
            ToolCall(
                "p-2",
                "search_evidence",
                {"query": "training certificate", "evidence_type": "training_record"},
            ),
            ToolCall(
                "p-3", "search_evidence", {"query": "coordinated disclosure severity remediation"}
            ),
        ]
    return "Policy text gathered.", []


def compose(system: list[SystemBlock], user: str) -> str:
    """A draft that, on purpose, leaks another tenant's name and a token so the scrub is tested."""
    return (
        "## CTL-VULN-DISCLOSURE: Vulnerability Disclosure Policy\n\n"
        "1. Reporting. Email the security team with a description and steps to reproduce.\n"
        "2. Response timeline. We acknowledge every report within 5 business days.\n"
        "3. Remediation. Confirmed vulnerabilities are remediated according to severity.\n\n"
        "Reference: as Globex Corporation does for globex-example, and token"
        " ghp_A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8.\n"
    )
