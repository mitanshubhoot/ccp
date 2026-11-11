"""Exact reading of evidence documents, for agents (spec.md A-146).

Semantic search finds passages that mean something close to a query; it is weak on exact
strings (a CVE, an ARN, a control id, an email, a date) and cannot walk a document's
structure. These functions do both, over one evidence observation (a version), and every
result carries the version id and [start, end) offsets into the page (or evidence) text,
the same offsets chunks carry (A-145):

    find(evidence, text= | kind=)   literal (case-insensitive) or typed-pattern matches
    outline(evidence)               headings (markdown, numbered, upper-case lines) and tables
    read(evidence, start, end)      the exact source text of a span, at most MAX_SPAN chars
    covering(evidence, ...)         the chunks a span falls in, recomputed by the chunker

Chunk ids are recomputed from the observation's text by the deterministic chunker, so they
are the ids the index holds (or held) for that version without a database read.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from ccp.normalize.entities import Evidence
from ccp.retrieval.chunker import ChunkDraft, chunk_evidence

MAX_SPAN = 4000
SNIPPET = 100
MAX_TITLE = 80

Kind = Literal["cve", "arn", "email", "date", "control_id"]
_MONTH = r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)[a-z]*\.?"
PATTERNS: dict[str, re.Pattern[str]] = {
    "cve": re.compile(r"\bCVE-\d{4}-\d{4,7}\b", re.IGNORECASE),
    "arn": re.compile(r"\barn:aws[a-z-]*:[a-z0-9-]*:[a-z0-9-]*:\d{0,12}:[^\s,;\"'<>)\]|]+"),
    "email": re.compile(r"\b[\w.+-]+@[\w-]+(?:\.[\w-]+)*\.[A-Za-z]{2,}\b"),
    "date": re.compile(
        rf"\b\d{{4}}-\d{{2}}-\d{{2}}\b"
        rf"|\b{_MONTH} \d{{1,2}}, \d{{4}}\b"
        rf"|\b\d{{1,2}} {_MONTH} \d{{4}}\b"
    ),
    # CCP's control ids, SOC 2 criteria (CC6.1) and ISO 27001 Annex A (A.5.15, A.8.2.3)
    "control_id": re.compile(
        r"\bCTL-[A-Z0-9-]*[A-Z0-9]\b|\bCC\d{1,2}\.\d{1,2}\b|\bA\.\d+(?:\.\d+)+\b"
    ),
}


def pages(evidence: Evidence) -> list[tuple[int | None, str]]:
    if evidence.page_texts is not None:
        return [(i, t) for i, t in enumerate(evidence.page_texts, start=1)]
    return [(None, evidence.text)]


def page_text(evidence: Evidence, page: int | None) -> str | None:
    for number, body in pages(evidence):
        if number == page:
            return body
    return None


@dataclass(frozen=True)
class Match:
    evidence_entity_id: str
    version: str
    evidence_type: str
    page: int | None
    start: int
    end: int
    match: str
    snippet: str


def find(
    evidence: Sequence[Evidence],
    *,
    text: str | None = None,
    kind: str | None = None,
    limit: int = 10,
) -> tuple[list[Match], int]:
    """Matches in document order, at most `limit`, and how many there were in all."""
    if (text is None) == (kind is None):
        raise ValueError("give exactly one of text and kind")
    pattern = (
        re.compile(re.escape(text), re.IGNORECASE) if text is not None else PATTERNS[str(kind)]
    )
    out: list[Match] = []
    total = 0
    for e in sorted(evidence, key=lambda x: x.entity_id):
        for page, body in pages(e):
            for m in pattern.finditer(body):
                total += 1
                if len(out) < limit:
                    out.append(
                        Match(
                            evidence_entity_id=e.entity_id,
                            version=e.source_event_id,
                            evidence_type=e.evidence_type.value,
                            page=page,
                            start=m.start(),
                            end=m.end(),
                            match=m.group(0),
                            snippet=body[max(0, m.start() - SNIPPET) : m.end() + SNIPPET],
                        )
                    )
    return out, total


@dataclass(frozen=True)
class Entry:
    kind: Literal["heading", "table"]
    level: int
    title: str
    page: int | None
    start: int
    end: int


_MARKDOWN = re.compile(r"^(#{1,6})\s+(.+?)\s*#*$")
_NUMBERED = re.compile(r"^((?:\d+\.)+\d*|\d+\))\s+\S")


def _heading(line: str) -> tuple[int, str] | None:
    md = _MARKDOWN.match(line)
    if md:
        return len(md.group(1)), md.group(2)
    numbered = _NUMBERED.match(line)
    if numbered and len(line) <= 120:
        return numbered.group(1).rstrip(".)").count(".") + 1, line
    letters = [c for c in line if c.isalpha()]
    if len(letters) >= 3 and line.isupper() and len(line) <= MAX_TITLE:
        return 1, line
    return None


def outline(evidence: Evidence) -> list[Entry]:
    entries: list[Entry] = []
    for page, body in pages(evidence):
        pos = 0
        table: tuple[int, int, str] | None = None  # start, end, first row
        for line in body.splitlines(keepends=True):
            stripped = line.strip()
            start = pos + len(line) - len(line.lstrip())
            end = start + len(stripped)
            pos += len(line)
            if stripped.startswith("|"):
                table = (table[0], end, table[2]) if table else (start, end, stripped)
                continue
            if table is not None:
                entries.append(Entry("table", 0, table[2][:MAX_TITLE], page, table[0], table[1]))
                table = None
            found = _heading(stripped) if stripped else None
            if found is not None:
                entries.append(Entry("heading", found[0], found[1][:MAX_TITLE], page, start, end))
        if table is not None:
            entries.append(Entry("table", 0, table[2][:MAX_TITLE], page, table[0], table[1]))
    return entries


@dataclass(frozen=True)
class Span:
    page: int | None
    start: int
    end: int
    text: str
    truncated: bool


def read(evidence: Evidence, start: int, end: int, page: int | None = None) -> Span | None:
    body = page_text(evidence, page)
    if body is None:
        return None
    start = max(0, min(start, len(body)))
    end = max(start, min(end, len(body)))
    truncated = end - start > MAX_SPAN
    if truncated:
        end = start + MAX_SPAN
    return Span(page, start, end, body[start:end], truncated)


def covering(evidence: Evidence, page: int | None, start: int, end: int) -> list[ChunkDraft]:
    """The chunks of this version that overlap [start, end) on a page."""
    return [
        d
        for d in chunk_evidence(evidence)
        if d.page == page
        and d.start_offset is not None
        and d.end_offset is not None
        and d.start_offset < max(end, start + 1)
        and d.end_offset > start
    ]


def chunks_of(evidence: Evidence) -> list[ChunkDraft]:
    """Every chunk of this version, in order (the ids the index holds or held for it)."""
    return chunk_evidence(evidence)
