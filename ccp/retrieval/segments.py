"""Segments of a document version (spec.md A-161): the units a model cites by id.

A model never quotes; it names segment ids (or, for a JSON document, JSON Pointers) and
code renders the quote from the stored text, so a citation cannot be paraphrased into
something the document does not say. Segmentation is deterministic and versioned, one
stored set per document version (its text's hash):

    p<page>.h<n>               a heading line
    p<page>.s<n>               a sentence of running text
    p<page>.t<t>.r<r>.c<c>     one cell of a pipe table (header row r0)

Offsets are within the page's text, so a pointer can highlight the span on the page.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from typing import Any, Literal

from ccp.storage.scoped import ScopedDB

SEGMENTER_VERSION = "seg_v1"
SegmentKind = Literal["heading", "sentence", "cell"]
_HEADING = re.compile(r"^\s{0,3}#{1,6}\s+\S")
_TABLE_ROW = re.compile(r"^\s*\|.*\|\s*$")
_TABLE_RULE = re.compile(r"^\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)*\|?\s*$")
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9\"'(\[])")


@dataclass(frozen=True)
class Segment:
    segment_id: str
    kind: SegmentKind
    page: int  # 1-based
    start: int  # offsets within the page's text
    end: int
    text: str


def _sentences(page: int, text: str, base: int, counter: list[int]) -> list[Segment]:
    out: list[Segment] = []
    at = 0
    pieces = _SENTENCE_END.split(text)
    for piece in pieces:
        found = text.find(piece, at)
        stripped = piece.strip()
        if found < 0 or not stripped:
            continue
        lead = len(piece) - len(piece.lstrip())
        start = base + found + lead
        counter[0] += 1
        out.append(
            Segment(
                f"p{page}.s{counter[0]}", "sentence", page, start, start + len(stripped), stripped
            )
        )
        at = found + len(piece)
    return out


def _cells(page: int, table: int, rows: list[tuple[int, str]]) -> list[Segment]:
    out: list[Segment] = []
    r = 0
    for offset, line in rows:
        if _TABLE_RULE.match(line):
            continue
        inner_start = line.index("|") + 1
        inner_end = line.rindex("|")
        c = 0
        cursor = inner_start
        for raw in line[inner_start:inner_end].split("|"):
            cell = raw.strip()
            lead = len(raw) - len(raw.lstrip())
            if cell:
                start = offset + cursor + lead
                out.append(
                    Segment(
                        f"p{page}.t{table}.r{r}.c{c}", "cell", page, start, start + len(cell), cell
                    )
                )
            cursor += len(raw) + 1
            c += 1
        r += 1
    return out


def segment_page(page: int, text: str) -> list[Segment]:
    out: list[Segment] = []
    sentences = [0]
    headings = 0
    tables = 0
    lines = text.split("\n")
    offsets: list[int] = []
    at = 0
    for line in lines:
        offsets.append(at)
        at += len(line) + 1
    paragraph: list[tuple[int, str]] = []

    def flush() -> None:
        if not paragraph:
            return
        start = paragraph[0][0]
        end = paragraph[-1][0] + len(paragraph[-1][1])
        out.extend(_sentences(page, text[start:end], start, sentences))
        paragraph.clear()

    i = 0
    while i < len(lines):
        line = lines[i]
        if _TABLE_ROW.match(line):
            flush()
            rows: list[tuple[int, str]] = []
            while i < len(lines) and _TABLE_ROW.match(lines[i]):
                rows.append((offsets[i], lines[i]))
                i += 1
            tables += 1
            out.extend(_cells(page, tables, rows))
            continue
        if _HEADING.match(line):
            flush()
            headings += 1
            stripped = line.strip()
            start = offsets[i] + (len(line) - len(line.lstrip()))
            out.append(
                Segment(
                    f"p{page}.h{headings}", "heading", page, start, start + len(stripped), stripped
                )
            )
        elif not line.strip():
            flush()
        else:
            paragraph.append((offsets[i], line))
        i += 1
    flush()
    return out


def segment(pages: Sequence[str]) -> list[Segment]:
    return [s for n, text in enumerate(pages, 1) for s in segment_page(n, text)]


def text_sha256(pages: Sequence[str]) -> str:
    return hashlib.sha256("\f".join(pages).encode()).hexdigest()


# -- the stored set ----------------------------------------------------------------------------


@dataclass(frozen=True)
class SegmentSet:
    evidence_entity_id: str
    text_sha256: str
    version: str
    segments: tuple[Segment, ...]

    def by_id(self) -> dict[str, Segment]:
        return {s.segment_id: s for s in self.segments}


def pages_of(text: str, page_texts: Sequence[str] | None) -> list[str]:
    return list(page_texts) if page_texts else [text]


def ensure(
    db: ScopedDB, evidence_entity_id: str, text: str, page_texts: Sequence[str] | None = None
) -> SegmentSet:
    """The document version's segments, made and stored once."""
    pages = pages_of(text, page_texts)
    digest = text_sha256(pages)
    row = db.fetch_one(
        "SELECT segments FROM segment_sets WHERE evidence_entity_id = %s AND text_sha256 = %s"
        " AND version = %s",
        (evidence_entity_id, digest, SEGMENTER_VERSION),
    )
    if row is not None:
        raw = json.loads(row[0]) if isinstance(row[0], str) else row[0]
        return SegmentSet(
            evidence_entity_id, digest, SEGMENTER_VERSION, tuple(Segment(**s) for s in raw)
        )
    made = tuple(segment(pages))
    db.execute(
        "INSERT INTO segment_sets (tenant_id, evidence_entity_id, text_sha256, version, segments)"
        " VALUES (%s, %s, %s, %s, %s) ON CONFLICT DO NOTHING",
        (
            db.tenant.tenant_id,
            evidence_entity_id,
            digest,
            SEGMENTER_VERSION,
            json.dumps([asdict(s) for s in made]),
        ),
    )
    return SegmentSet(evidence_entity_id, digest, SEGMENTER_VERSION, made)


# -- citations by id ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Rendered:
    quote: str
    pointers: tuple[dict[str, Any], ...]
    unknown: tuple[str, ...]


def render(found: SegmentSet, segment_ids: Sequence[str]) -> Rendered:
    """The quote code renders for cited segment ids, with a pointer per segment; ids the
    set does not hold are returned, never rendered."""
    index = found.by_id()
    known = [index[i] for i in segment_ids if i in index]
    unknown = tuple(i for i in segment_ids if i not in index)
    return Rendered(
        " … ".join(s.text for s in known),
        tuple(
            {
                "evidence_entity_id": found.evidence_entity_id,
                "text_sha256": found.text_sha256,
                "segment_id": s.segment_id,
                "page": s.page,
                "start": s.start,
                "end": s.end,
            }
            for s in known
        ),
        unknown,
    )


def resolve_pointer(document: Any, pointer: str) -> Any:
    """A JSON Pointer (RFC 6901) into a JSON document; KeyError when it names nothing."""
    if pointer == "":
        return document
    if not pointer.startswith("/"):
        raise KeyError(pointer)
    node = document
    for raw in pointer[1:].split("/"):
        token = raw.replace("~1", "/").replace("~0", "~")
        if isinstance(node, list):
            if not token.isdigit() or int(token) >= len(node):
                raise KeyError(pointer)
            node = node[int(token)]
        elif isinstance(node, dict) and token in node:
            node = node[token]
        else:
            raise KeyError(pointer)
    return node
