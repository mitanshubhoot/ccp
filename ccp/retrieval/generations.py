"""Index generations and versioned chunks (spec.md A-145).

    generation_id(model, chunker)   "gen-" + sha256(model | chunker)[:16]: the same embedding model
                                    and chunker always name the same generation
    ensure(db, model)               the generation row, written once
    active(db, as_of)               the generation serving at a time (now by default)
    activate(db, gen, reason, gate) insert-only and chained: 'first', 'gate_passed' or 'forced'
    latest_sets(db, gen)            {evidence entity: its newest set's content hash; None: retired}
    record_set / record_tombstone   a new set of chunk rows for one evidence observation, or its end
    project(db, gen)                rebuild the serving `chunks` from a generation's newest sets
    as_of_source()                  the SQL relation of a generation's chunks as of a time

Nothing here updates or deletes a version: a changed document adds a set, a retired one adds a
tombstone, and a re-embedding adds a generation. The serving `chunks` table is the one copy
that changes, and it can always be rebuilt from the versions.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import psycopg
from ulid import ULID

from ccp.normalize.entities import Evidence
from ccp.retrieval.chunker import CHUNKER_VERSION, ChunkDraft
from ccp.retrieval.embeddings import vector_literal
from ccp.storage.scoped import ScopedDB


@dataclass(frozen=True)
class Generation:
    generation_id: str
    embed_model: str
    chunker_version: str
    activated_at: datetime | None = None
    reason: str | None = None


def generation_id(embed_model: str, chunker_version: str = CHUNKER_VERSION) -> str:
    digest = hashlib.sha256(f"{embed_model}|{chunker_version}".encode()).hexdigest()
    return f"gen-{digest[:16]}"


def ensure(db: ScopedDB, embed_model: str, chunker_version: str = CHUNKER_VERSION) -> str:
    gen = generation_id(embed_model, chunker_version)
    db.execute(
        "INSERT INTO index_generations (tenant_id, generation_id, embed_model, chunker_version)"
        " VALUES (%s, %s, %s, %s) ON CONFLICT DO NOTHING",
        (db.tenant.tenant_id, gen, embed_model, chunker_version),
    )
    return gen


def describe(db: ScopedDB, gen: str) -> Generation | None:
    row = db.fetch_one(
        "SELECT generation_id, embed_model, chunker_version FROM index_generations"
        " WHERE generation_id = %s",
        (gen,),
    )
    return Generation(str(row[0]), str(row[1]), str(row[2])) if row else None


def active(db: ScopedDB, as_of: datetime | None = None) -> Generation | None:
    row = db.fetch_one(
        "SELECT a.generation_id, g.embed_model, g.chunker_version, a.activated_at, a.reason"
        " FROM index_activations a JOIN index_generations g USING (tenant_id, generation_id)"
        " WHERE (%(as_of)s::timestamptz IS NULL OR a.activated_at <= %(as_of)s)"
        " ORDER BY a.activated_at DESC, a.activation_id DESC LIMIT 1",
        {"as_of": as_of},
    )
    if row is None:
        return None
    return Generation(str(row[0]), str(row[1]), str(row[2]), row[3], str(row[4]))


def activate(db: ScopedDB, gen: str, reason: str, gate: dict[str, Any]) -> str:
    activation_id = str(ULID())
    db.execute(
        "INSERT INTO index_activations (tenant_id, activation_id, generation_id, reason, gate)"
        " VALUES (%s, %s, %s, %s, %s)",
        (db.tenant.tenant_id, activation_id, gen, reason, json.dumps(gate, default=str)),
    )
    return activation_id


def history(db: ScopedDB) -> list[dict[str, Any]]:
    rows = db.fetch_all(
        "SELECT g.generation_id, g.embed_model, g.chunker_version, g.created_at,"
        " (SELECT count(*) FROM chunk_sets s WHERE s.generation_id = g.generation_id),"
        " (SELECT count(*) FROM chunk_versions v WHERE v.generation_id = g.generation_id),"
        " (SELECT max(a.activated_at) FROM index_activations a"
        "   WHERE a.generation_id = g.generation_id)"
        " FROM index_generations g ORDER BY g.created_at"
    )
    serving = active(db)
    return [
        {
            "generation_id": str(r[0]),
            "embed_model": str(r[1]),
            "chunker_version": str(r[2]),
            "created_at": r[3],
            "sets": int(r[4]),
            "chunk_versions": int(r[5]),
            "last_activated_at": r[6],
            "active": serving is not None and serving.generation_id == str(r[0]),
        }
        for r in rows
    ]


def content_sha(drafts: Sequence[ChunkDraft], contexts: dict[str, str | None]) -> str:
    """What a set holds: chunk ids (content hashes), offsets, scope and enrichment."""
    material = "\n".join(
        "|".join(
            str(x)
            for x in (
                d.chunk_id,
                d.start_offset,
                d.end_offset,
                d.subject_entity_id,
                d.source_ref,
                hashlib.sha256((contexts.get(d.chunk_id) or "").encode()).hexdigest()[:16],
            )
        )
        for d in drafts
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def latest_sets(db: ScopedDB, gen: str) -> dict[str, str | None]:
    rows = db.fetch_all(
        "SELECT DISTINCT ON (evidence_entity_id) evidence_entity_id, content_sha256,"
        " evidence_event_id IS NULL FROM chunk_sets WHERE generation_id = %s"
        " ORDER BY evidence_entity_id, recorded_at DESC, set_id DESC",
        (gen,),
    )
    return {str(r[0]): None if r[2] else str(r[1]) for r in rows}


def record_set(
    cur: psycopg.Cursor[Any],
    tenant_id: str,
    gen: str,
    embed_model: str,
    evidence: Evidence,
    drafts: Sequence[ChunkDraft],
    contexts: dict[str, str | None],
    vectors: dict[str, list[float]],
) -> str:
    set_id = str(ULID())
    cur.execute(
        "INSERT INTO chunk_sets (tenant_id, set_id, generation_id, evidence_entity_id,"
        " evidence_event_id, evidence_observed_at, content_sha256, chunk_count)"
        " VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
        (
            tenant_id,
            set_id,
            gen,
            evidence.entity_id,
            evidence.source_event_id,
            evidence.observed_at,
            content_sha(drafts, contexts),
            len(drafts),
        ),
    )
    for d in drafts:
        cur.execute(
            "INSERT INTO chunk_versions (tenant_id, set_id, chunk_id, generation_id,"
            " evidence_entity_id, evidence_type, subject_entity_id, source_ref, page, ordinal,"
            " start_offset, end_offset, text, context, embedding, embed_model, chunker_version,"
            " text_sha256)"
            " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::vector,%s,%s,%s)",
            (
                tenant_id,
                set_id,
                d.chunk_id,
                gen,
                d.evidence_entity_id,
                d.evidence_type,
                d.subject_entity_id,
                d.source_ref,
                d.page,
                d.ordinal,
                d.start_offset,
                d.end_offset,
                d.text,
                contexts.get(d.chunk_id),
                vector_literal(vectors[d.chunk_id]),
                embed_model,
                CHUNKER_VERSION,
                d.text_sha256,
            ),
        )
    return set_id


def record_tombstone(cur: psycopg.Cursor[Any], tenant_id: str, gen: str, entity_id: str) -> str:
    set_id = str(ULID())
    cur.execute(
        "INSERT INTO chunk_sets (tenant_id, set_id, generation_id, evidence_entity_id,"
        " evidence_event_id, content_sha256, chunk_count) VALUES (%s, %s, %s, %s, NULL, '', 0)",
        (tenant_id, set_id, gen, entity_id),
    )
    return set_id


# A generation's chunks as of a time: each evidence entity's newest set recorded by then (a
# tombstone has no rows, so a retired document drops out). Distractors are never versioned.
AS_OF_SOURCE = """(
  SELECT v.chunk_id, v.evidence_entity_id, v.evidence_type, v.subject_entity_id, v.source_ref,
         v.page, v.ordinal, v.start_offset, v.end_offset, v.text, v.context, v.tsv, v.embedding,
         v.embed_model, false AS is_distractor
  FROM chunk_versions v
  JOIN (
    SELECT DISTINCT ON (evidence_entity_id) set_id FROM chunk_sets
    WHERE generation_id = %(gen)s
      AND (%(as_of)s::timestamptz IS NULL OR recorded_at <= %(as_of)s)
    ORDER BY evidence_entity_id, recorded_at DESC, set_id DESC
  ) latest USING (set_id)
)"""


def project(db: ScopedDB, gen: str) -> int:
    """Make `chunks` serve generation `gen`: its newest set of every current evidence entity."""
    with db.transaction() as cur:
        cur.execute("DELETE FROM chunks WHERE NOT is_distractor")
        cur.execute(
            "INSERT INTO chunks (chunk_id, tenant_id, evidence_entity_id, evidence_type,"
            " subject_entity_id, source_ref, page, ordinal, text, context, embedding,"
            " embed_model, chunker_version, text_sha256, is_distractor, generation_id,"
            " start_offset, end_offset)"
            " SELECT v.chunk_id, v.tenant_id, v.evidence_entity_id, v.evidence_type,"
            " v.subject_entity_id, v.source_ref, v.page, v.ordinal, v.text, v.context,"
            " v.embedding, v.embed_model, v.chunker_version, v.text_sha256, false,"
            " v.generation_id, v.start_offset, v.end_offset"
            " FROM chunk_versions v JOIN (SELECT DISTINCT ON (evidence_entity_id) set_id"
            "   FROM chunk_sets WHERE generation_id = %s"
            "   ORDER BY evidence_entity_id, recorded_at DESC, set_id DESC) latest USING (set_id)",
            (gen,),
        )
        written = cur.rowcount
        cur.execute(
            "DELETE FROM chunks WHERE is_distractor AND embed_model <>"
            " (SELECT embed_model FROM index_generations WHERE generation_id = %s)",
            (gen,),
        )
    return written


def chunk_from_versions(db: ScopedDB, chunk_ids: Sequence[str]) -> dict[str, dict[str, Any]]:
    """Chunks no longer served, read back from any generation (a chunk id pins its text)."""
    if not chunk_ids:
        return {}
    rows = db.fetch_all(
        "SELECT DISTINCT ON (chunk_id) chunk_id, evidence_entity_id, evidence_type,"
        " subject_entity_id, page, source_ref, text, start_offset, end_offset"
        " FROM chunk_versions WHERE chunk_id = ANY(%s) ORDER BY chunk_id, set_id DESC",
        (list(chunk_ids),),
    )
    keys = (
        "chunk_id",
        "evidence_entity_id",
        "evidence_type",
        "subject_entity_id",
        "page",
        "source_ref",
        "text",
        "start_offset",
        "end_offset",
    )
    return {str(r[0]): dict(zip(keys, r, strict=True)) for r in rows}
