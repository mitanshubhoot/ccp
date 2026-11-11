"""The retrieval index over current evidence (spec.md A-43, A-145).

    refresh(db, ...)   chunk every current evidence row (with offsets) -> enrich (flag) ->
                       embed (cached) -> record a new chunk set, in the embedder's generation,
                       for every document whose chunks changed (a tombstone for one retired)
                       -> if that generation serves: upsert `chunks`, delete what is gone
    rebuild            re-project `chunks` from scratch; with another embedding model it also
                       forces that generation to serve, recorded as a 'forced' activation

Unchanged text re-indexes as a no-op because chunk ids are content hashes. The versions are
the record; `chunks` is the serving copy of the active generation. Refreshing with a model
that is not the active generation's writes a candidate generation and leaves `chunks` alone:
`ccp index reembed` gates it against the serving one and switches only on a pass.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ccp.config import Settings
from ccp.llm.budget import BudgetTracker, RunBudget
from ccp.llm.client import LLMClient
from ccp.llm.errors import RetryableModelError, TerminalModelError
from ccp.normalize import repository
from ccp.normalize.entities import Evidence
from ccp.observability import steps
from ccp.retrieval import cache, contextual, generations
from ccp.retrieval.chunker import CHUNKER_VERSION, ChunkDraft, chunk_evidence
from ccp.retrieval.embeddings import EmbeddingClient, vector_literal
from ccp.storage.scoped import ScopedDB


@dataclass
class IndexSummary:
    embed_model: str
    chunker_version: str = CHUNKER_VERSION
    generation_id: str = ""
    serving: bool = True
    evidence_rows: int = 0
    chunks: int = 0
    upserted: int = 0
    unchanged: int = 0
    deleted: int = 0
    sets_written: int = 0
    tombstones: int = 0
    embedded: int = 0
    embedding_cache_hits: int = 0
    enriched: int = 0
    enrichment_cache_hits: int = 0
    usd: float = 0.0
    events: list[str] = field(default_factory=list)


def _embedding_input(context: str | None, text: str) -> str:
    return f"{context}\n\n{text}" if context else text


def _target(db: ScopedDB, gen: str, rebuild: bool, summary: IndexSummary) -> bool:
    """Whether the embedder's generation serves; activates it when it is the first or forced."""
    current = generations.active(db)
    if current is None:
        generations.activate(db, gen, "first", {"note": "the tenant's first index"})
        summary.events.append(f"generation_activated:first:{gen}")
        return True
    if current.generation_id == gen:
        return True
    if rebuild:
        generations.activate(
            db, gen, "forced", {"by": "ccp index rebuild", "replaced": current.generation_id}
        )
        summary.events.append(f"generation_activated:forced:{gen}")
        return True
    summary.events.append(f"candidate_generation:{gen}")
    return False


def refresh(
    db: ScopedDB,
    settings: Settings,
    embedder: EmbeddingClient,
    llm: LLMClient | None = None,
    *,
    contextual_enrichment: bool | None = None,
    rebuild: bool = False,
) -> IndexSummary:
    summary = IndexSummary(embed_model=embedder.model_id)
    want_context = (
        settings.retrieval_contextual if contextual_enrichment is None else contextual_enrichment
    )
    if want_context and llm is None:
        summary.events.append("contextual_unavailable:no_llm_client")
        want_context = False
    with steps.step(
        "index.refresh",
        layer="retrieval",
        tenant=db.tenant,
        inputs={"embed_model": embedder.model_id, "rebuild": rebuild, "contextual": want_context},
    ) as s:
        gen = generations.ensure(db, embedder.model_id)
        summary.generation_id = gen
        serving = summary.serving = _target(db, gen, rebuild, summary)
        if serving and rebuild:
            summary.deleted += db.execute("DELETE FROM chunks WHERE NOT is_distractor")
        if serving:  # distractors embedded with another model would poison the index
            summary.deleted += db.execute(
                "DELETE FROM chunks WHERE is_distractor AND embed_model <> %s",
                (embedder.model_id,),
            )
        evidence = repository.current_evidence(db)
        summary.evidence_rows = len(evidence)
        by_entity: dict[str, list[ChunkDraft]] = {}
        observations: dict[str, Evidence] = {}
        documents: dict[str, str] = {}
        for row in evidence:
            documents[row.entity_id] = row.text
            observations[row.entity_id] = row
            by_entity[row.entity_id] = chunk_evidence(row)
        drafts = [d for ds in by_entity.values() for d in ds]
        summary.chunks = len(drafts)

        existing = {
            str(r[0]): (str(r[1]), r[2], r[3])
            for r in db.fetch_all(
                "SELECT chunk_id, embed_model, context, generation_id FROM chunks"
                " WHERE NOT is_distractor"
            )
        }
        pending = (
            [
                d
                for d in drafts
                if d.chunk_id not in existing
                or existing[d.chunk_id][0] != embedder.model_id
                or (want_context and existing[d.chunk_id][1] is None)
                or existing[d.chunk_id][2] != gen
            ]
            if serving
            else []
        )

        # contexts are model-independent: reuse what the serving copy holds, enrich the rest
        contexts: dict[str, str | None] = {
            d.chunk_id: existing[d.chunk_id][1] if d.chunk_id in existing else None for d in drafts
        }
        to_enrich = [d for d in drafts if want_context and contexts[d.chunk_id] is None]
        if to_enrich and llm is not None:
            tracker = BudgetTracker(
                RunBudget.from_settings(settings).lowered(max_seconds=3600, max_usd=5.0)
            )
            for draft in to_enrich:
                doc = documents[draft.evidence_entity_id]
                key = cache.enrichment_key(
                    settings.ccp_tier2_model,
                    contextual.PROMPT_VERSION,
                    contextual.document_sha(doc),
                    draft.text_sha256,
                )
                hit = cache.get_enrichment(db, key)
                if hit is not None:
                    contexts[draft.chunk_id] = hit
                    summary.enrichment_cache_hits += 1
                    continue
                try:
                    enrichment = contextual.enrich(
                        llm,
                        document=doc,
                        chunk_text=draft.text,
                        chunk_id=draft.chunk_id,
                        evidence_type=draft.evidence_type,
                        model=settings.ccp_tier2_model,
                        tracker=tracker,
                    )
                except (RetryableModelError, TerminalModelError) as exc:
                    summary.events.append(f"contextual_unavailable:{type(exc).__name__}")
                    continue
                contexts[draft.chunk_id] = enrichment.context
                summary.enriched += 1
                summary.usd += enrichment.usd
                cache.put_enrichment(
                    db, key, settings.ccp_tier2_model, contextual.PROMPT_VERSION, enrichment.context
                )

        latest = generations.latest_sets(db, gen)
        changed = [
            entity
            for entity, ds in by_entity.items()
            if latest.get(entity) != generations.content_sha(ds, contexts)
        ]
        retired = sorted(e for e, sha in latest.items() if sha is not None and e not in by_entity)
        versioned = [d for e in changed for d in by_entity[e]]
        summary.unchanged = len(drafts) - len(pending if serving else versioned)

        compute: dict[str, ChunkDraft] = {}
        for d in [*pending, *versioned]:
            compute.setdefault(d.chunk_id, d)
        inputs = {cid: _embedding_input(contexts[cid], d.text) for cid, d in compute.items()}
        keys = {
            cid: cache.embedding_key(embedder.model_id, "document", text)
            for cid, text in inputs.items()
        }
        cached = cache.get_embeddings(db, list(set(keys.values())))
        summary.embedding_cache_hits = sum(1 for k in keys.values() if k in cached)
        missing = [cid for cid, k in keys.items() if k not in cached]
        vectors: dict[str, list[float]] = {
            cid: cached[keys[cid]] for cid in inputs if keys[cid] in cached
        }
        if missing:
            embedded = embedder.embed([inputs[cid] for cid in missing], "document")
            summary.embedded = len(missing)
            summary.usd += embedded.usd
            new_entries: dict[str, list[float]] = {}
            for cid, vector in zip(missing, embedded.vectors, strict=True):
                vectors[cid] = vector
                new_entries[keys[cid]] = vector
            cache.put_embeddings(db, embedder.model_id, new_entries)

        tenant_id = db.tenant.tenant_id
        with db.transaction() as cur:
            for entity in changed:
                generations.record_set(
                    cur,
                    tenant_id,
                    gen,
                    embedder.model_id,
                    observations[entity],
                    by_entity[entity],
                    contexts,
                    vectors,
                )
                summary.sets_written += 1
            for entity in retired:
                generations.record_tombstone(cur, tenant_id, gen, entity)
                summary.tombstones += 1
            if serving:
                for draft in pending:
                    cur.execute(
                        "INSERT INTO chunks (chunk_id, tenant_id, evidence_entity_id,"
                        " evidence_type, subject_entity_id, source_ref, page, ordinal, text,"
                        " context, embedding, embed_model, chunker_version, text_sha256,"
                        " is_distractor, generation_id, start_offset, end_offset)"
                        " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::vector,%s,%s,%s,false,"
                        "%s,%s,%s)"
                        " ON CONFLICT (chunk_id) DO UPDATE SET embedding = EXCLUDED.embedding,"
                        " embed_model = EXCLUDED.embed_model, context = EXCLUDED.context,"
                        " generation_id = EXCLUDED.generation_id,"
                        " start_offset = EXCLUDED.start_offset,"
                        " end_offset = EXCLUDED.end_offset, indexed_at = now()",
                        (
                            draft.chunk_id,
                            tenant_id,
                            draft.evidence_entity_id,
                            draft.evidence_type,
                            draft.subject_entity_id,
                            draft.source_ref,
                            draft.page,
                            draft.ordinal,
                            draft.text,
                            contexts[draft.chunk_id],
                            vector_literal(vectors[draft.chunk_id]),
                            embedder.model_id,
                            CHUNKER_VERSION,
                            draft.text_sha256,
                            gen,
                            draft.start_offset,
                            draft.end_offset,
                        ),
                    )
                    summary.upserted += 1
                wanted = [d.chunk_id for d in drafts]
                cur.execute(
                    "DELETE FROM chunks WHERE NOT is_distractor AND NOT (chunk_id = ANY(%s))",
                    (wanted,),
                )
                summary.deleted += cur.rowcount
        for event in summary.events:
            s.event(event)
        s.set_output(
            {
                "generation_id": gen,
                "serving": serving,
                "chunks": summary.chunks,
                "upserted": summary.upserted,
                "unchanged": summary.unchanged,
                "deleted": summary.deleted,
                "sets_written": summary.sets_written,
                "tombstones": summary.tombstones,
                "embedded": summary.embedded,
                "cache_hits": summary.embedding_cache_hits,
                "enriched": summary.enriched,
                "usd": round(summary.usd, 6),
            }
        )
    return summary


def stats(db: ScopedDB) -> dict[str, object]:
    by_type = db.fetch_all(
        "SELECT evidence_type, count(*) FROM chunks WHERE NOT is_distractor"
        " GROUP BY evidence_type ORDER BY evidence_type"
    )
    models = db.fetch_all("SELECT embed_model, count(*) FROM chunks GROUP BY embed_model")
    distractors = db.fetch_one("SELECT count(*) FROM chunks WHERE is_distractor")
    contexts = db.fetch_one("SELECT count(*) FROM chunks WHERE context IS NOT NULL")
    serving = generations.active(db)
    return {
        "chunks_by_type": {str(r[0]): int(r[1]) for r in by_type},
        "embed_models": {str(r[0]): int(r[1]) for r in models},
        "distractors": int(distractors[0]) if distractors else 0,
        "with_context": int(contexts[0]) if contexts else 0,
        "active_generation": serving.generation_id if serving else None,
        "generations": len(generations.history(db)),
    }


def chunk_count(db: ScopedDB) -> int:
    row = db.fetch_one("SELECT count(*) FROM chunks WHERE NOT is_distractor")
    return int(row[0]) if row else 0
