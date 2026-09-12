"""Contextual chunk enrichment at index time (spec.md A-46, flag RETRIEVAL_CONTEXTUAL): one
short model call per chunk with the whole document in the cached prefix. The document is
untrusted text, so it goes through the same fence as evidence. Cached by content hash."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from ccp.llm.budget import BudgetTracker
from ccp.llm.client import LLMClient, SystemBlock
from ccp.llm.prompts import load_prompt
from ccp.security import prompt_guard
from ccp.security.prompt_guard import FramedChunk

PROMPT_VERSION = "contextual_v1"
MAX_DOCUMENT_CHARS = 60_000


@dataclass(frozen=True)
class Enrichment:
    context: str
    usd: float
    tokens: int


def document_sha(document: str) -> str:
    return hashlib.sha256(document.encode("utf-8")).hexdigest()


def enrich(
    llm: LLMClient,
    *,
    document: str,
    chunk_text: str,
    chunk_id: str,
    evidence_type: str,
    model: str,
    tracker: BudgetTracker,
) -> Enrichment:
    prompt = load_prompt(PROMPT_VERSION)
    nonce = prompt_guard.new_nonce()
    doc = document[:MAX_DOCUMENT_CHARS]
    framed_doc = prompt_guard.frame_evidence([FramedChunk("document", doc, evidence_type)], nonce)
    framed_chunk = prompt_guard.frame_evidence(
        [FramedChunk(chunk_id, chunk_text, evidence_type)], nonce
    )
    system = [
        SystemBlock(prompt.text),
        SystemBlock(f"The whole document:\n\n{framed_doc.text}", cache=True),
    ]
    user = (
        f"{prompt_guard.fence_declaration(nonce)}\n\nThe chunk to situate:\n\n{framed_chunk.text}\n"
        "Answer with the succinct context only."
    )
    completion = llm.complete(
        system=system, user=user, model=model, tracker=tracker, max_tokens=200
    )
    return Enrichment(
        context=completion.text.strip(),
        usd=completion.usage.usd,
        tokens=completion.usage.input_tokens + completion.usage.output_tokens,
    )
