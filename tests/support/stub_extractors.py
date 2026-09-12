"""Test-only stand-ins for the extraction model.

They read exactly the prompt the model would read (the nonce-fenced evidence in the user
turn), find the fields with plain regexes, and cite verbatim substrings of the chunk they
found them in. They are deliberately literal: they never follow instructions inside the
evidence, so the injection fixture is ignored the way a well-behaved model ignores it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel

from ccp.evaluators.extraction.base import Cited
from ccp.evaluators.extraction.gh4 import DisclosurePolicyFields
from ccp.evaluators.extraction.hr3 import TrainingRecordFields
from ccp.llm.client import SystemBlock

_FENCE = re.compile(
    r"<<EVIDENCE-(?P<nonce>[0-9a-f]+) chunk_id=(?P<chunk>\S+) type=(?P<type>\S+) subject=\S+"
    r" page=\S+>>\n(?P<body>.*?)\n<<END-(?P=nonce)>>",
    re.DOTALL,
)
_TIMELINE = re.compile(r"within \d+ (?:business |working )?(?:days|hours)", re.IGNORECASE)
_EMPLOYEE = re.compile(r"Employee: (?P<name>.+?) \(employee id (?P<eid>[^)]+)\)")


@dataclass(frozen=True)
class Block:
    chunk_id: str
    evidence_type: str
    body: str

    def line_with(self, needle: str) -> str | None:
        for line in self.body.splitlines():
            if needle.lower() in line.lower():
                return line.strip()
        return None


def blocks(user: str) -> list[Block]:
    return [Block(m["chunk"], m["type"], m["body"]) for m in _FENCE.finditer(user)]


def extract_gh4(user: str) -> DisclosurePolicyFields:
    found = blocks(user)
    policy = next(
        (b for b in found if re.search(r"vulnerab|security policy|report", b.body, re.IGNORECASE)),
        None,
    )
    if policy is None:
        return DisclosurePolicyFields(policy_exists=False, timeline_stated=False)
    first_line = policy.body.strip().splitlines()[0].strip()
    fields: dict[str, Any] = {
        "policy_exists": True,
        "policy_exists_citation": Cited(chunk_id=policy.chunk_id, quote=first_line[:200]),
        "timeline_stated": False,
    }
    for block in found:
        match = _TIMELINE.search(block.body)
        if match:
            line = block.line_with(match.group(0)) or match.group(0)
            fields.update(
                timeline_stated=True,
                timeline_stated_citation=Cited(chunk_id=block.chunk_id, quote=line[:300]),
                timeline_text=match.group(0),
                timeline_text_citation=Cited(chunk_id=block.chunk_id, quote=line[:300]),
            )
            break
    return DisclosurePolicyFields(**fields)


def extract_hr3(user: str) -> TrainingRecordFields:
    found = blocks(user)
    who = _EMPLOYEE.search(user)
    name = who["name"] if who else ""
    eid = who["eid"] if who else ""
    fields: dict[str, Any] = {
        "subject_name_in_document": False,
        "completion_status": "unknown",
    }
    for block in found:
        naming = block.line_with("certifies that") or block.line_with("Learner:")
        if naming and name and name.lower() in naming.lower() and eid.lower() in naming.lower():
            fields["subject_name_in_document"] = True
            fields["subject_name_in_document_citation"] = Cited(
                chunk_id=block.chunk_id, quote=naming[:300]
            )
        elif naming and "subject_name_in_document_citation" not in fields:
            fields["subject_name_in_document_citation"] = Cited(
                chunk_id=block.chunk_id, quote=naming[:300]
            )
        status_line = block.line_with("Status:")
        if status_line:
            lowered = status_line.lower()
            if "in progress" in lowered:
                status = "in_progress"
            elif lowered.startswith("status: completed"):
                status = "completed"
            else:
                status = "unknown"
            fields["completion_status"] = status
            fields["completion_status_citation"] = Cited(chunk_id=block.chunk_id, quote=status_line)
        date_line = block.line_with("Completion date:")
        if date_line:
            match = re.search(r"\d{4}-\d{2}-\d{2}", date_line)
            if match:
                fields["completion_date"] = match.group(0)
                fields["completion_date_citation"] = Cited(chunk_id=block.chunk_id, quote=date_line)
        provider_line = block.line_with("Provider:")
        if provider_line:
            fields["provider"] = provider_line.split(":", 1)[1].strip()
            fields["provider_citation"] = Cited(chunk_id=block.chunk_id, quote=provider_line)
    return TrainingRecordFields(**fields)


def stub_extract(schema: type[Any], system: list[SystemBlock], user: str) -> BaseModel:
    if schema is DisclosurePolicyFields:
        return extract_gh4(user)
    if schema is TrainingRecordFields:
        return extract_hr3(user)
    raise RuntimeError(f"no stub extractor for {schema.__name__}")


def stub_fabricating(schema: type[Any], system: list[SystemBlock], user: str) -> BaseModel:
    """A model that invents its citations: an unknown chunk id and a quote that is not there."""
    found = blocks(user)
    real = found[0].chunk_id if found else "nope"
    if schema is DisclosurePolicyFields:
        return DisclosurePolicyFields(
            policy_exists=True,
            policy_exists_citation=Cited(chunk_id="fabricated-chunk-id", quote="anything"),
            timeline_stated=True,
            timeline_stated_citation=Cited(chunk_id=real, quote="we respond within 1 hour"),
            timeline_text="within 1 hour",
            timeline_text_citation=Cited(chunk_id=real, quote="we respond within 1 hour"),
        )
    raise RuntimeError("fabricating stub only speaks GH-4")
