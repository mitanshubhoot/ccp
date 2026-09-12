"""PDF resource caps (spec.md A-54, security row S20)."""

from __future__ import annotations

from pathlib import Path

import pytest
from fpdf import FPDF

from ccp.connectors.manual.pdf_text import PdfLimits, extract_pages

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


def _pdf_with_pages(path: Path, pages: int) -> Path:
    pdf = FPDF()
    pdf.set_font("Helvetica", size=12)
    for i in range(pages):
        pdf.add_page()
        pdf.cell(0, 10, f"Page {i + 1} of {pages}")
    pdf.output(str(path))
    return path


def test_ok_pdf_returns_one_text_block_per_page(tmp_path: Path) -> None:
    result = extract_pages(_pdf_with_pages(tmp_path / "three.pdf", 3), PdfLimits())
    assert result.status == "ok"
    assert result.page_count == 3
    assert [p.strip() for p in result.pages] == ["Page 1 of 3", "Page 2 of 3", "Page 3 of 3"]


def test_size_cap_rejects_before_parsing(tmp_path: Path) -> None:
    result = extract_pages(_pdf_with_pages(tmp_path / "big.pdf", 1), PdfLimits(max_bytes=10))
    assert result.status == "too_large"
    assert result.pages == []


def test_page_cap_rejects_with_the_count(tmp_path: Path) -> None:
    result = extract_pages(_pdf_with_pages(tmp_path / "many.pdf", 3), PdfLimits(max_pages=2))
    assert result.status == "too_many_pages"
    assert result.page_count == 3
    assert result.pages == []


def test_timeout_is_reported_not_raised(tmp_path: Path) -> None:
    result = extract_pages(
        _pdf_with_pages(tmp_path / "slow.pdf", 1), PdfLimits(timeout_seconds=0.001)
    )
    assert result.status == "timeout"


def test_unreadable_file_is_reported_not_raised(tmp_path: Path) -> None:
    junk = tmp_path / "junk.pdf"
    junk.write_bytes(b"%PDF-1.7 this is not really a pdf")
    result = extract_pages(junk, PdfLimits())
    assert result.status == "unreadable"


def test_no_text_layer_fixture_is_classified() -> None:
    result = extract_pages(FIXTURES / "no_text_layer.pdf", PdfLimits())
    assert result.status == "no_text_layer"
    assert result.page_count == 1


@pytest.mark.parametrize("bad", [0, -1])
def test_limits_reject_nonsense_values(bad: int) -> None:
    with pytest.raises(ValueError):
        PdfLimits(max_pages=bad)
