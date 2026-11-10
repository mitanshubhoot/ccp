"""Content-hash caches for embeddings and contextual enrichment (spec.md D25), tenant-scoped
under RLS like everything else. A cache hit is a step event, never a silent shortcut."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence

from ccp.retrieval.embeddings import parse_vector, vector_literal
from ccp.storage.scoped import ScopedDB


def embedding_key(model: str, input_type: str, text: str) -> str:
    return hashlib.sha256(f"{model}|{input_type}|{text}".encode()).hexdigest()


def enrichment_key(model: str, prompt_version: str, document_sha: str, chunk_sha: str) -> str:
    return hashlib.sha256(
        f"{model}|{prompt_version}|{document_sha}|{chunk_sha}".encode()
    ).hexdigest()


def get_embeddings(db: ScopedDB, keys: Sequence[str]) -> dict[str, list[float]]:
    if not keys:
        return {}
    rows = db.fetch_all(
        "SELECT cache_key, embedding::text FROM embedding_cache WHERE cache_key = ANY(%s)",
        (list(keys),),
    )
    return {str(r[0]): parse_vector(str(r[1])) for r in rows}


def put_embeddings(db: ScopedDB, model: str, entries: Mapping[str, Sequence[float]]) -> None:
    if not entries:
        return
    with db.transaction() as cur:
        for key, vector in entries.items():
            cur.execute(
                "INSERT INTO embedding_cache (cache_key, tenant_id, embed_model, embedding)"
                " VALUES (%s, %s, %s, %s::vector) ON CONFLICT (tenant_id, cache_key) DO NOTHING",
                (key, db.tenant.tenant_id, model, vector_literal(vector)),
            )


def get_enrichment(db: ScopedDB, key: str) -> str | None:
    row = db.fetch_one("SELECT context FROM enrichment_cache WHERE cache_key = %s", (key,))
    return str(row[0]) if row else None


def put_enrichment(db: ScopedDB, key: str, model: str, prompt_version: str, context: str) -> None:
    db.execute(
        "INSERT INTO enrichment_cache (cache_key, tenant_id, model, prompt_version, context)"
        " VALUES (%s, %s, %s, %s, %s) ON CONFLICT (tenant_id, cache_key) DO NOTHING",
        (key, db.tenant.tenant_id, model, prompt_version, context),
    )
