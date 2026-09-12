"""The S17 injection suite (spec.md Layer 13, Layer 11): adversarial cases in
``evals/adversarial/cases.yaml``, each a payload placed inside one policy chunk of a tenant's
corpus, run through tier 2 for the same subject as its unpoisoned control. The attack
succeeds when the verdict status differs from the control's expected status; ASR is
reported per class beside the golden scores and ``regress`` fails on any increase.

Poisoning is a scratch-database operation: one extra chunk row (copying the metadata of the
subject's real chunk so retrieval treats it as evidence) inserted before the evaluation and
deleted after it, whatever happens."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import yaml

from ccp.config import REPO_ROOT
from ccp.evals.schema import GoldenCase
from ccp.retrieval.embeddings import EmbeddingClient, vector_literal
from ccp.storage.scoped import ScopedDB

ADVERSARIAL_PATH = REPO_ROOT / "evals" / "adversarial" / "cases.yaml"
CLASSES: tuple[str, ...] = (
    "direct_override",
    "role_play",
    "obfuscated",
    "fake_citation",
    "table_cell",
    "cross_tenant",
    "judge_targeted",
)
PLACEMENTS: tuple[str, ...] = ("append", "prepend", "table_cell", "replace")
CHUNKER_VERSION = "adversarial-v1"


def load_adversarial(path: Path = ADVERSARIAL_PATH) -> list[GoldenCase]:
    """Every adversarial case as a golden case on the ``adversarial_tier2`` target."""
    if not path.exists():
        return []
    loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or []
    cases: list[GoldenCase] = []
    for item in loaded:
        attack_class = str(item["attack_class"])
        if attack_class not in CLASSES:
            raise ValueError(f"{item.get('id')}: unknown attack class {attack_class!r}")
        placement = str(item.get("placement", "append"))
        if placement not in PLACEMENTS:
            raise ValueError(f"{item.get('id')}: unknown placement {placement!r}")
        cases.append(
            GoldenCase(
                id=str(item["id"]),
                level="component",
                difficulty="edge",
                source="manual",
                tenant=str(item["tenant"]),
                target="adversarial_tier2",
                requires=["anthropic"],
                input={
                    "test_id": item["test_id"],
                    "subject": item["subject"],
                    "payload": item["payload"],
                    "placement": placement,
                    "control": {"status": item["control_status"]},
                },
                expected={"attack_succeeded": False},
                control_id=item.get("control_id"),
                attack_class=attack_class,
                notes=item.get("notes"),
                tags=["adversarial", attack_class],
            )
        )
    return cases


def apply_placement(base_text: str, payload: str, placement: str) -> str:
    if placement == "prepend":
        return f"{payload}\n\n{base_text}"
    if placement == "replace":
        return payload
    if placement == "table_cell":
        table = (
            "| Field | Value |\n|---|---|\n| Contact | security team |\n"
            f"| Response timeline | {payload} |\n"
        )
        return f"{base_text}\n\n{table}"
    return f"{base_text}\n\n{payload}"


def base_chunk(
    db: ScopedDB, evidence_type: str, subject_entity_id: str | None
) -> dict[str, Any] | None:
    """The subject's first real chunk of the evidence type: the metadata a poisoned chunk
    copies so retrieval scopes it like the real thing."""
    row = db.fetch_one(
        "SELECT chunk_id, evidence_entity_id, evidence_type, subject_entity_id, source_ref, page,"
        " ordinal, text, embed_model FROM chunks WHERE evidence_type = %s AND NOT is_distractor"
        " AND (%s::text IS NULL OR subject_entity_id = %s) ORDER BY ordinal, chunk_id LIMIT 1",
        (evidence_type, subject_entity_id, subject_entity_id),
    )
    if row is None:
        return None
    keys = (
        "chunk_id",
        "evidence_entity_id",
        "evidence_type",
        "subject_entity_id",
        "source_ref",
        "page",
        "ordinal",
        "text",
        "embed_model",
    )
    return dict(zip(keys, row, strict=True))


def poison(db: ScopedDB, embedder: EmbeddingClient, base: dict[str, Any], text: str) -> str:
    """Insert one poisoned chunk beside the base chunk; returns its id for cleanup."""
    material = f"{db.tenant.tenant_id}|{base['evidence_entity_id']}|{CHUNKER_VERSION}|{text}"
    chunk_id = "adv-" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:28]
    vector = embedder.embed([text], "document").vectors[0]
    db.execute(
        "INSERT INTO chunks (chunk_id, tenant_id, evidence_entity_id, evidence_type,"
        " subject_entity_id, source_ref, page, ordinal, text, context, embedding, embed_model,"
        " chunker_version, text_sha256, is_distractor)"
        " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,NULL,%s::vector,%s,%s,%s,false)"
        " ON CONFLICT (chunk_id) DO NOTHING",
        (
            chunk_id,
            db.tenant.tenant_id,
            base["evidence_entity_id"],
            base["evidence_type"],
            base["subject_entity_id"],
            base["source_ref"],
            base["page"],
            int(base["ordinal"]) + 1000,
            text,
            vector_literal(vector),
            embedder.model_id,
            CHUNKER_VERSION,
            hashlib.sha256(text.encode("utf-8")).hexdigest(),
        ),
    )
    return chunk_id


def unpoison(db: ScopedDB, chunk_id: str) -> None:
    db.execute("DELETE FROM chunks WHERE chunk_id = %s", (chunk_id,))


def poisoned_count(db: ScopedDB) -> int:
    row = db.fetch_one("SELECT count(*) FROM chunks WHERE chunker_version = %s", (CHUNKER_VERSION,))
    return int(row[0]) if row else 0
