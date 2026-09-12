"""S13 (spec.md Layer 13): an upload cannot claim another tenant. The tenant comes from the
uploads directory the file sits in; a claim in the manifest or the front matter is ignored
and recorded as a guardrail event, and the other tenant's index is untouched."""

from __future__ import annotations

from pathlib import Path

import pytest

from ccp.config import Settings
from ccp.ingest.landing import ingest_tenant
from ccp.normalize import repository
from ccp.normalize.consumer import normalize_all
from ccp.observability import steps
from ccp.retrieval import index
from ccp.retrieval.embeddings import HashingEmbeddingClient
from tests.support.pipeline import prepared_tenants

pytestmark = pytest.mark.db

MANIFEST = (
    "uploads:\n"
    "  - file: note.md\n"
    "    tenant: globex\n"
    "    evidence_type: policy_document\n"
    "    title: Claimed Note\n"
    "    issued_at: 2026-01-01T00:00:00Z\n"
    "    uploaded_at: 2026-09-01T00:00:00Z\n"
)
NOTE = (
    "---\ntenant: globex\ntitle: Claimed Note\n---\n"
    "# Claimed Note\n\nA note about access reviews.\n"
)


def test_an_upload_cannot_claim_another_tenant(base_settings: Settings, tmp_path: Path) -> None:
    with prepared_tenants(base_settings, "ccp_s13") as (settings, dbs):
        acme, globex = dbs["acme"], dbs["globex"]
        index.refresh(globex, settings, HashingEmbeddingClient(), contextual_enrichment=False)
        globex_chunks_before = index.chunk_count(globex)
        assert globex_chunks_before > 0
        run_settings = settings.model_copy(update={"ccp_data_dir": tmp_path})
        root = run_settings.uploads_dir / "acme"
        root.mkdir(parents=True)
        (root / "note.md").write_text(NOTE, encoding="utf-8")
        (root / "manifest.yaml").write_text(MANIFEST, encoding="utf-8")
        ingest_tenant(run_settings, acme, acme.tenant, connectors=("manual",), source="live")
        normalize_all(acme, run_settings)
        mine = [e for e in repository.current_evidence(acme) if e.title == "Claimed Note"]
        assert len(mine) == 1 and mine[0].tenant_id == "acme"
        logged = [
            line
            for line in steps.read_lines()
            if any(
                str(event).startswith("upload_tenant_claim_ignored:")
                for event in line.get("ccp.guardrail_events") or []
            )
        ]
        assert logged
        events = {e for line in logged for e in line["ccp.guardrail_events"]}
        assert "upload_tenant_claim_ignored:manifest:globex" in events
        assert "upload_tenant_claim_ignored:front_matter:globex" in events
        assert not [e for e in repository.current_evidence(globex) if e.title == "Claimed Note"]
        assert index.chunk_count(globex) == globex_chunks_before
