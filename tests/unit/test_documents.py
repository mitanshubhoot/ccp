"""MT9 (A-146): the exact-reading functions behind find_exact, outline and read_span."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from ccp.normalize.entities import Evidence
from ccp.retrieval import documents
from ccp.tenancy import TenantId
from ccp.types import EvidenceType

TEXT = """# Incident Response Policy

Owner: security@acme.example. Approved 2026-03-14 and reviewed on March 2, 2026.

## 1. Scope
Covers CVE-2026-12345 and the bucket arn:aws:s3:::acme-logs (see CTL-ACCESS-REVOKE, CC6.1
and A.5.15).

| system | owner |
|---|---|
| GitHub | platform |

2.1 Escalation
SEVERITY LEVELS
"""


def _evidence(text: str = TEXT, pages: tuple[str, ...] | None = None) -> Evidence:
    return Evidence(
        tenant_id=TenantId("acme"),
        entity_id="manual:evidence:ir.md",
        observed_at=datetime(2026, 3, 14, tzinfo=UTC),
        source_connector="manual",
        source_event_id="evt-1",
        evidence_type=EvidenceType.policy_document,
        title="IR policy",
        text=text,
        raw_ref="raw/ir.md",
        page_texts=pages,
    )


@pytest.mark.parametrize(
    ("kind", "expected"),
    [
        ("cve", ["CVE-2026-12345"]),
        ("arn", ["arn:aws:s3:::acme-logs"]),
        ("email", ["security@acme.example"]),
        ("date", ["2026-03-14", "March 2, 2026"]),
        ("control_id", ["CTL-ACCESS-REVOKE", "CC6.1", "A.5.15"]),
    ],
)
def test_typed_patterns(kind: str, expected: list[str]) -> None:
    matches, total = documents.find([_evidence()], kind=kind)
    assert [m.match for m in matches] == expected and total == len(expected)
    for m in matches:
        assert TEXT[m.start : m.end] == m.match and m.version == "evt-1"


def test_literal_matches_ignore_case_and_cap() -> None:
    matches, total = documents.find([_evidence()], text="OWNER", limit=1)
    assert total == 2 and len(matches) == 1 and matches[0].match == "Owner"
    with pytest.raises(ValueError, match="exactly one"):
        documents.find([_evidence()], text="x", kind="cve")


def test_outline_finds_headings_and_tables_with_offsets() -> None:
    entries = documents.outline(_evidence())
    titles = [(e.kind, e.level, e.title) for e in entries]
    assert titles == [
        ("heading", 1, "Incident Response Policy"),
        ("heading", 2, "1. Scope"),
        ("table", 0, "| system | owner |"),
        ("heading", 2, "2.1 Escalation"),
        ("heading", 1, "SEVERITY LEVELS"),
    ]
    table = entries[2]
    assert TEXT[table.start : table.end].endswith("| GitHub | platform |")


def test_read_clips_and_respects_pages() -> None:
    paged = _evidence("unused", pages=("page one text", "page two text"))
    span = documents.read(paged, 5, 10_000, page=2)
    assert span is not None and span.text == "two text" and not span.truncated
    assert documents.read(paged, 0, 5, page=3) is None
    long = documents.read(_evidence("x" * 9000), 0, 9000)
    assert long is not None and long.truncated and len(long.text) == documents.MAX_SPAN
    inside = documents.covering(_evidence(), None, TEXT.index("CVE"), TEXT.index("CVE") + 3)
    assert inside and all(c.start_offset is not None for c in inside)
