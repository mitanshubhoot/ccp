"""Page-aware chunker (spec.md A-43). Deterministic, versioned, content-addressed.

    evidence text ──► blocks (blank-line separated; a markdown table stays one block)
                 ──► packed windows of about `target` characters, never crossing a page
                 ──► ChunkDraft(page, ordinal, text, chunk_id = sha256(...)[:32])

PDF evidence carries one text per page, so a chunk always knows its page and a citation can
name it; the split-table access review is the golden case for that.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

from ccp.normalize.entities import Evidence

CHUNKER_VERSION = "v1"
TARGET_CHARS = 700
MAX_CHARS = 1100

_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")


@dataclass(frozen=True)
class ChunkDraft:
    chunk_id: str
    evidence_entity_id: str
    evidence_type: str
    subject_entity_id: str | None
    source_ref: str
    page: int | None
    ordinal: int
    text: str
    text_sha256: str


def chunk_id_for(
    tenant_id: str,
    evidence_entity_id: str,
    page: int | None,
    ordinal: int,
    text: str,
    version: str = CHUNKER_VERSION,
) -> str:
    material = f"{tenant_id}|{evidence_entity_id}|{version}|{page}|{ordinal}|{text}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]


def _blocks(text: str) -> list[str]:
    """Blank-line separated blocks; consecutive table rows stay together."""
    blocks: list[str] = []
    current: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            if current:
                blocks.append("\n".join(current))
                current = []
            continue
        current.append(stripped)
    if current:
        blocks.append("\n".join(current))
    return blocks


def _split_long(block: str, max_chars: int) -> list[str]:
    if len(block) <= max_chars:
        return [block]
    pieces: list[str] = []
    buf = ""
    for sentence in _SENTENCE_END.split(block):
        if buf and len(buf) + 1 + len(sentence) > max_chars:
            pieces.append(buf)
            buf = sentence
        else:
            buf = f"{buf} {sentence}".strip()
    if buf:
        pieces.append(buf)
    out: list[str] = []
    for piece in pieces:  # a single sentence longer than the cap is hard-split
        while len(piece) > max_chars:
            out.append(piece[:max_chars])
            piece = piece[max_chars:]
        out.append(piece)
    return out


def chunk_text(text: str, target: int = TARGET_CHARS, max_chars: int = MAX_CHARS) -> list[str]:
    windows: list[str] = []
    buf = ""
    for block in _blocks(text):
        for piece in _split_long(block, max_chars):
            if buf and len(buf) + 2 + len(piece) > target:
                windows.append(buf)
                buf = piece
            else:
                buf = f"{buf}\n\n{piece}" if buf else piece
    if buf:
        windows.append(buf)
    return windows


def chunk_evidence(evidence: Evidence) -> list[ChunkDraft]:
    pages: list[tuple[int | None, str]]
    if evidence.page_texts is not None:
        pages = [(i, t) for i, t in enumerate(evidence.page_texts, start=1)]
    else:
        pages = [(None, evidence.text)]
    drafts: list[ChunkDraft] = []
    ordinal = 0
    for page, page_text in pages:
        for window in chunk_text(page_text):
            drafts.append(
                ChunkDraft(
                    chunk_id=chunk_id_for(
                        evidence.tenant_id, evidence.entity_id, page, ordinal, window
                    ),
                    evidence_entity_id=evidence.entity_id,
                    evidence_type=evidence.evidence_type.value,
                    subject_entity_id=evidence.subject_entity_id,
                    source_ref=evidence.raw_ref,
                    page=page,
                    ordinal=ordinal,
                    text=window,
                    text_sha256=hashlib.sha256(window.encode("utf-8")).hexdigest(),
                )
            )
            ordinal += 1
    return drafts
