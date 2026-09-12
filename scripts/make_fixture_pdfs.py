"""Regenerate the committed fixture PDFs from their text (dev only; needs fpdf2).

    uv run python scripts/make_fixture_pdfs.py

Outputs:
  fixtures/acme/uploads/security-awareness-2026-atorres.pdf   a training certificate
  fixtures/globex/uploads/access-review-2026-h1.pdf           an access review whose scope
                                                              table is split across a page break
  tests/fixtures/no_text_layer.pdf                            a drawing-only PDF, no text objects
The creation date is pinned so the bytes are reproducible.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from fpdf import FPDF

ROOT = Path(__file__).resolve().parents[1]
PINNED = datetime(2026, 9, 1, 9, 0, 0, tzinfo=UTC)


def _pdf() -> FPDF:
    pdf = FPDF(format="A4")
    pdf.set_creation_date(PINNED)
    pdf.set_auto_page_break(auto=True, margin=15)
    pdf.set_margins(20, 20, 20)
    return pdf


def training_certificate(path: Path) -> None:
    pdf = _pdf()
    pdf.add_page()
    pdf.set_font("Helvetica", "B", 18)
    pdf.cell(0, 12, "Security Awareness Training Certificate", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "", 12)
    pdf.ln(4)
    pdf.multi_cell(
        0,
        7,
        "This certifies that Ana Torres (employee E1058) completed the Acme Security Awareness "
        "Training 2026, covering phishing, credential hygiene, data handling, and incident "
        "reporting.",
    )
    pdf.ln(4)
    for line in (
        "Status: Completed",
        "Completion date: 2026-03-14",
        "Provider: KnowBe4",
        "Score: 96%",
        "Certificate id: KB4-2026-031441",
    ):
        pdf.cell(0, 7, line, new_x="LMARGIN", new_y="NEXT")
    pdf.ln(6)
    pdf.set_font("Helvetica", "I", 10)
    pdf.multi_cell(
        0, 6, "Issued by Acme Corp Security. Valid for twelve months from the completion date."
    )
    pdf.output(str(path))


def access_review(path: Path) -> None:
    pdf = _pdf()
    pdf.add_page()
    pdf.set_font("Helvetica", "B", 16)
    pdf.cell(0, 10, "H1 2026 User Access Review", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "", 11)
    pdf.cell(0, 7, "Globex Corporation - Security and Compliance", new_x="LMARGIN", new_y="NEXT")
    pdf.cell(
        0,
        7,
        "Review date: 2026-08-01    Reviewer: Ines Duarte    Approver: Tom Baker",
        new_x="LMARGIN",
        new_y="NEXT",
    )
    pdf.ln(4)
    pdf.set_font("Helvetica", "B", 12)
    pdf.cell(0, 8, "1. Purpose", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "", 11)
    pdf.multi_cell(
        0,
        6,
        "This review verifies that access to every system tagged critical in the Globex asset "
        "inventory is limited to personnel with a current business need, that privileged roles "
        "are justified, and that leavers have been deprovisioned within the 24-hour service "
        "level. It was performed against the identity provider export of 2026-07-28 and the "
        "GitHub organization membership as of the same date.",
    )
    pdf.ln(2)
    pdf.set_font("Helvetica", "B", 12)
    pdf.cell(0, 8, "2. Method", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "", 11)
    pdf.multi_cell(
        0,
        6,
        "Each system owner received the list of accounts and roles for their system and confirmed, "
        "removed, or reduced each one. Confirmations were recorded in the compliance tracker with "
        "the owner's name and the date. Removals were executed the same day and verified by a "
        "second reviewer. Service accounts were reviewed against the account registry to confirm "
        "a named individual owner.",
    )
    pdf.ln(2)
    pdf.set_font("Helvetica", "B", 12)
    pdf.cell(0, 8, "3. Scope", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "", 11)
    pdf.multi_cell(
        0,
        6,
        "The following systems were in scope for this review. Each row records the owner, "
        "the review date, and the outcome.",
    )
    # Push the table to the bottom of page 1 so the rows continue on page 2. This is the
    # page-aware chunking eval case: a citation must be able to say which page a row is on.
    pdf.set_y(pdf.h - 15 - 26)
    widths = (40, 40, 35, 55)
    pdf.set_font("Helvetica", "B", 10)
    for width, label in zip(widths, ("System", "Owner", "Reviewed on", "Result"), strict=True):
        pdf.cell(width, 8, label, border=1)
    pdf.ln(8)
    pdf.set_font("Helvetica", "", 10)
    rows = (
        ("GitHub", "Tom Baker", "2026-07-29", "1 removal (ytanaka), confirmed"),
        ("Okta", "Ines Duarte", "2026-07-30", "no changes"),
        ("Payroll-GX", "Nadia Hassan", "2026-07-31", "2 role reductions"),
    )
    for row in rows:
        for width, value in zip(widths, row, strict=True):
            pdf.cell(width, 8, value, border=1)
        pdf.ln(8)
    pdf.ln(4)
    pdf.set_font("Helvetica", "B", 12)
    pdf.cell(0, 8, "4. Conclusion", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "", 11)
    pdf.multi_cell(
        0,
        6,
        "All three systems tagged critical (GitHub, Okta, Payroll-GX) were reviewed. One account "
        "was removed and two privileged roles were reduced. No shared account without a named "
        "owner was found. The next review is due 2027-02-01.",
    )
    pdf.output(str(path))


def no_text_layer(path: Path) -> None:
    pdf = _pdf()
    pdf.add_page()
    pdf.set_fill_color(200, 200, 200)
    pdf.rect(30, 30, 150, 200, style="F")
    pdf.output(str(path))


def main() -> None:
    targets = {
        ROOT
        / "fixtures"
        / "acme"
        / "uploads"
        / "security-awareness-2026-atorres.pdf": training_certificate,
        ROOT / "fixtures" / "globex" / "uploads" / "access-review-2026-h1.pdf": access_review,
        ROOT / "tests" / "fixtures" / "no_text_layer.pdf": no_text_layer,
    }
    for path, make in targets.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        make(path)
        print(f"wrote {path.relative_to(ROOT)} ({path.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
