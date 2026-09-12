"""PDF text extraction in a subprocess with size, page, and time caps (spec.md A-54).

pypdf runs in a child process so a hostile or malformed file can only cost the caps,
never wedge the upload consumer. The child prints one JSON object; the parent classifies
the result. A file that fails any cap is rejected, never parsed further.

    extract_pages(path, limits)
      ├── size > max_bytes            -> status "too_large"
      ├── child exceeds timeout       -> status "timeout"
      ├── child crashes / bad PDF     -> status "unreadable"
      ├── page_count > max_pages      -> status "too_many_pages"
      ├── every page empty            -> status "no_text_layer"
      └── otherwise                   -> status "ok", pages = [text per page]
"""

from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

PdfStatus = Literal["ok", "too_large", "timeout", "unreadable", "too_many_pages", "no_text_layer"]


@dataclass(frozen=True)
class PdfLimits:
    max_bytes: int = 25_000_000
    max_pages: int = 500
    timeout_seconds: float = 60.0

    def __post_init__(self) -> None:
        if self.max_bytes <= 0 or self.max_pages <= 0 or self.timeout_seconds <= 0:
            raise ValueError("PDF limits must be positive")


@dataclass(frozen=True)
class PdfExtraction:
    status: PdfStatus
    page_count: int = 0
    pages: list[str] = field(default_factory=list)
    detail: str | None = None

    @property
    def text(self) -> str:
        return "\n\n".join(self.pages)


DEFAULT_LIMITS = PdfLimits()


def extract_pages(path: Path, limits: PdfLimits = DEFAULT_LIMITS) -> PdfExtraction:
    size = path.stat().st_size
    if size > limits.max_bytes:
        return PdfExtraction("too_large", detail=f"{size} bytes > {limits.max_bytes}")
    try:
        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "ccp.connectors.manual.pdf_text",
                str(path),
                str(limits.max_pages),
            ],
            capture_output=True,
            text=True,
            timeout=limits.timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return PdfExtraction("timeout", detail=f"> {limits.timeout_seconds}s")
    if completed.returncode != 0:
        return PdfExtraction("unreadable", detail=completed.stderr.strip()[-500:] or "child failed")
    try:
        result = json.loads(completed.stdout)
    except json.JSONDecodeError:
        return PdfExtraction("unreadable", detail="child produced no JSON")
    page_count = int(result["page_count"])
    if page_count > limits.max_pages:
        return PdfExtraction(
            "too_many_pages", page_count=page_count, detail=f"{page_count} > {limits.max_pages}"
        )
    pages = [str(p) for p in result["pages"]]
    if not any(p.strip() for p in pages):
        return PdfExtraction("no_text_layer", page_count=page_count, pages=pages)
    return PdfExtraction("ok", page_count=page_count, pages=pages)


def _child(path: str, max_pages: int) -> dict[str, object]:
    from pypdf import PdfReader

    reader = PdfReader(path)
    page_count = len(reader.pages)
    pages: list[str] = []
    if page_count <= max_pages:
        for page in reader.pages:
            pages.append(page.extract_text() or "")
    return {"page_count": page_count, "pages": pages}


if __name__ == "__main__":  # pragma: no cover - exercised through the parent
    json.dump(_child(sys.argv[1], int(sys.argv[2])), sys.stdout)
