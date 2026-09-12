from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime

from ccp.config import Settings
from ccp.connectors.hris.fixture import HrisFixtureConnector
from ccp.tenancy import TenantContext


def _pull(settings: Settings, tenant: str):  # type: ignore[no-untyped-def]
    conn = HrisFixtureConnector(settings.ccp_fixtures_dir)
    return list(conn.pull(TenantContext.for_id(settings, tenant)))


def test_acme_event_types_and_counts(base_settings: Settings) -> None:
    counts = Counter(e.event_type for e in _pull(base_settings, "acme"))
    assert counts == {
        "hris.employee.snapshot": 8,
        "hris.hire": 8,
        "hris.role_change": 1,
        "hris.termination": 1,
        "hris.identity.snapshot": 9,
        "hris.account.snapshot": 4,
        "hris.asset.snapshot": 7,
        "hris.document": 9,
    }


def test_termination_event_for_jane_smith(base_settings: Settings) -> None:
    term = next(e for e in _pull(base_settings, "acme") if e.event_type == "hris.termination")
    assert term.source_event_id == "acme-hr-evt-0388"
    assert term.payload["employee_id"] == "E1042"
    assert term.source_ts == datetime(2026, 8, 20, 17, 0, tzinfo=UTC)


def test_documents_carry_front_matter_and_body(base_settings: Settings) -> None:
    docs = {
        e.payload["filename"]: e
        for e in _pull(base_settings, "acme")
        if e.event_type == "hris.document"
    }
    review = docs["access-review-2026-q2.md"]
    assert review.payload["document_type"] == "access_review_report"
    assert review.source_ts == datetime(2026, 7, 15, tzinfo=UTC)
    assert review.source_event_id.startswith("access_review_report:access-review-2026-q2.md:")
    assert "Payroll-SaaS" not in review.payload["text"]
    assert "| Datadog |" in review.payload["text"]
    training = docs["training-E1050.md"]
    assert training.payload["subject_employee_id"] == "E1050"
    assert "Sam Patil" in training.payload["text"]


def test_document_ids_change_when_bytes_change(base_settings: Settings, tmp_path) -> None:  # type: ignore[no-untyped-def]
    events = {
        e.payload["filename"]: e.source_event_id
        for e in _pull(base_settings, "acme")
        if e.event_type == "hris.document"
    }
    assert len(set(events.values())) == len(events)
    assert all(len(v.rsplit(":", 1)[1]) == 12 for v in events.values())


def test_globex_has_one_mfa_gap_and_one_offboarded_leaver(base_settings: Settings) -> None:
    events = _pull(base_settings, "globex")
    no_mfa = [
        e
        for e in events
        if e.event_type == "hris.identity.snapshot"
        and not e.payload["mfa_enabled"]
        and e.payload["status"] == "active"
    ]
    assert [e.payload["employee_id"] for e in no_mfa] == ["G2004"]
    term = next(e for e in events if e.event_type == "hris.termination")
    assert term.payload["employee_id"] == "G2005"
