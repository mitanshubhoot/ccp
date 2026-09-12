from __future__ import annotations

import logging
import shutil
from datetime import UTC, datetime
from pathlib import Path

import pytest

from ccp.config import Settings
from ccp.connectors.manual.pdf_text import PdfLimits
from ccp.connectors.manual.uploads import ManifestMissing, ManualUploadConnector
from ccp.connectors.registry import build_connector
from ccp.tenancy import TenantContext, TenantId

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


def _fixture_connector(settings: Settings) -> ManualUploadConnector:
    return ManualUploadConnector(lambda t: settings.ccp_fixtures_dir / t / "uploads", PdfLimits())


def test_acme_uploads_yield_pdf_pages_and_markdown(base_settings: Settings) -> None:
    events = list(
        _fixture_connector(base_settings).pull(TenantContext.for_id(base_settings, "acme"))
    )
    assert [e.event_type for e in events] == ["manual.document", "manual.document"]
    pdf, policy = events
    assert pdf.payload["pdf"]["status"] == "ok"
    assert pdf.payload["pdf"]["page_count"] == 1
    assert "Ana Torres" in pdf.payload["text"]
    assert "Completion date: 2026-03-14" in pdf.payload["text"]
    assert pdf.payload["evidence_type"] == "training_record"
    assert pdf.payload["subject_employee_id"] == "E1058"
    assert pdf.source_ts == datetime(2026, 9, 1, 9, 12, tzinfo=UTC)
    assert pdf.source_event_id.startswith("upload:security-awareness-2026-atorres.pdf:")
    assert policy.payload["evidence_type"] == "policy_document"
    assert policy.payload["front_matter"]["version"] == 4
    assert "5 business days" in policy.payload["text"]


def test_globex_access_review_pdf_table_spans_a_page_break(base_settings: Settings) -> None:
    events = list(
        _fixture_connector(base_settings).pull(TenantContext.for_id(base_settings, "globex"))
    )
    review = next(e for e in events if e.payload["filename"] == "access-review-2026-h1.pdf")
    pages = review.payload["pdf"]["pages"]
    assert review.payload["pdf"]["page_count"] == 2
    assert "GitHub" in pages[0] and "Reviewed on" in pages[0]
    assert "Payroll-GX" in pages[1] and "Payroll-GX" not in pages[0]


def test_no_text_layer_pdf_is_flagged_not_indexed(tmp_path: Path, base_settings: Settings) -> None:
    root = tmp_path / "acme"
    root.mkdir()
    shutil.copy(FIXTURES / "no_text_layer.pdf", root / "scan.pdf")
    (root / "manifest.yaml").write_text(
        "uploads:\n  - file: scan.pdf\n    evidence_type: training_record\n    title: Scan\n"
        "    issued_at: 2026-01-01T00:00:00Z\n    uploaded_at: 2026-09-01T00:00:00Z\n",
        encoding="utf-8",
    )
    conn = ManualUploadConnector(lambda t: tmp_path / t, PdfLimits())
    events = list(conn.pull(TenantContext.for_id(base_settings, "acme")))
    assert len(events) == 1
    assert events[0].payload["pdf"]["status"] == "no_text_layer"
    assert events[0].payload["text"].strip() == ""


def test_tenant_comes_from_the_directory_not_the_manifest(
    tmp_path: Path, base_settings: Settings, caplog: pytest.LogCaptureFixture
) -> None:
    root = tmp_path / "acme"
    root.mkdir()
    (root / "note.md").write_text("---\ntenant: globex\n---\n# Note\nbody\n", encoding="utf-8")
    (root / "stray.txt").write_text("not in manifest", encoding="utf-8")
    (root / "manifest.yaml").write_text(
        "uploads:\n  - file: note.md\n    tenant: globex\n"
        "    evidence_type: policy_document\n    title: Note\n"
        "    issued_at: 2026-01-01T00:00:00Z\n    uploaded_at: 2026-09-01T00:00:00Z\n",
        encoding="utf-8",
    )
    conn = ManualUploadConnector(lambda t: tmp_path / t, PdfLimits())
    with caplog.at_level(logging.WARNING):
        events = list(conn.pull(TenantContext.for_id(base_settings, "acme")))
    assert len(events) == 1
    assert events[0].tenant_id == TenantId("acme")
    assert (
        events[0].payload["manifest"]["tenant"] == "globex"
    )  # carried for the S13 guardrail, never used
    assert events[0].payload["front_matter"]["tenant"] == "globex"
    assert "stray.txt" in caplog.text and "not in the manifest" in caplog.text


def test_missing_manifest_is_an_error(tmp_path: Path, base_settings: Settings) -> None:
    (tmp_path / "acme").mkdir()
    conn = ManualUploadConnector(lambda t: tmp_path / t, PdfLimits())
    with pytest.raises(ManifestMissing):
        list(conn.pull(TenantContext.for_id(base_settings, "acme")))


def test_registry_builds_fixture_and_runtime_roots(base_settings: Settings) -> None:
    fixture = build_connector("manual", base_settings, "fixture")
    runtime = build_connector("manual", base_settings, "live")
    assert isinstance(fixture, ManualUploadConnector) and isinstance(runtime, ManualUploadConnector)
    with pytest.raises(ManifestMissing):
        list(runtime.pull(TenantContext.for_id(base_settings, "acme")))
