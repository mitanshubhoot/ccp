"""Admissibility of uploaded evidence, decided by code (spec.md A-161).

Before any model reads an upload, code checks what can be checked without one: is it
dated, is that date plausible and recent enough, does it cover a period when the type
needs one, does it name a known subject when it is about a person, and does it say who
issued it. A scan or screenshot is also held to the date that is visible in it. The
result is a list of reason codes, each blocking or a warning, and a provenance badge:

    customer_attested   an upload: the customer says this is what it is
    system_observed     read by CCP itself from a provider's API (or, later, CCP's own
                        capture of a console page with its URL and capture time)

A blocking reason keeps a model-transcribed document (a scan or a screenshot) out of the
index the evaluators search (ccp/retrieval/admission.py), because a picture of the wrong
day or the wrong person is exactly what a transcription cannot catch. Text and PDF uploads
are shown their reasons and are otherwise counted as before.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

import yaml

from ccp.knowledge.graph import KNOWLEDGE_DIR

RULES_FILE = KNOWLEDGE_DIR / "admissibility.yaml"
Badge = Literal["customer_attested", "system_observed"]
Severity = Literal["block", "warn"]
LIVE_CONNECTORS = frozenset({"github", "aws", "idp", "hris"})
_ISO = re.compile(r"\b(20\d\d)-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])\b")
_MONTHS = {
    m: i
    for i, m in enumerate(
        ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), 1
    )
}
_WORDY = re.compile(
    r"\b(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+(\d{1,2}),?\s+(20\d\d)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Rules:
    max_age_days: int
    needs_period: bool
    needs_subject: bool
    needs_issuer: bool
    visible_date_days: int


@lru_cache(maxsize=2)
def _load(path: Path = RULES_FILE) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return dict(raw.get("defaults") or {}), {
        str(k): dict(v or {}) for k, v in (raw.get("types") or {}).items()
    }


def rules_for(evidence_type: str) -> Rules:
    defaults, types = _load()
    merged = {**defaults, **types.get(evidence_type, {})}
    return Rules(
        int(merged.get("max_age_days", 400)),
        bool(merged.get("needs_period", False)),
        bool(merged.get("needs_subject", False)),
        bool(merged.get("needs_issuer", False)),
        int(merged.get("visible_date_days", 31)),
    )


@dataclass(frozen=True)
class Reason:
    code: str
    severity: Severity
    detail: str = ""


@dataclass(frozen=True)
class Admissibility:
    badge: Badge
    reasons: tuple[Reason, ...] = field(default=())

    @property
    def admissible(self) -> bool:
        return not any(r.severity == "block" for r in self.reasons)

    def as_dict(self) -> dict[str, Any]:
        return {
            "admissible": self.admissible,
            "badge": self.badge,
            "reasons": [r.__dict__ for r in self.reasons],
        }


def visible_dates(text: str) -> list[date]:
    """Dates written in a document's text: ISO dates and "Sep 1, 2026"."""
    found: list[date] = []
    for y, m, d in _ISO.findall(text):
        try:
            found.append(date(int(y), int(m), int(d)))
        except ValueError:
            continue
    for mon, d, y in _WORDY.findall(text):
        try:
            found.append(date(int(y), _MONTHS[mon[:3].lower()], int(d)))
        except (ValueError, KeyError):
            continue
    return found


def _parse(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day, tzinfo=UTC)
    if isinstance(value, str) and value:
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    return None


def transcribed(page_methods: tuple[str, ...] | None) -> bool:
    return any(m.startswith(("transcribed:", "blank:", "illegible:")) for m in page_methods or ())


def check(
    *,
    evidence_type: str,
    source_connector: str,
    issued_at: datetime | None,
    text: str,
    page_methods: tuple[str, ...] | None,
    subject_entity_id: str | None,
    manifest: dict[str, Any] | None,
    front_matter: dict[str, Any] | None,
    now: datetime,
    image: bool = False,
) -> Admissibility:
    """Every check code can make about one document version, as reason codes."""
    badge: Badge = "system_observed" if source_connector in LIVE_CONNECTORS else "customer_attested"
    rules = rules_for(evidence_type)
    manifest = manifest or {}
    reasons: list[Reason] = []
    image_like = transcribed(page_methods)
    if issued_at is None:
        reasons.append(Reason("no_issue_date", "block"))
    else:
        uploaded = _parse(manifest.get("uploaded_at")) or now
        if issued_at > max(now, uploaded) + timedelta(days=1):
            reasons.append(Reason("issued_in_future", "block", issued_at.date().isoformat()))
        age = (now - issued_at).days
        if age > rules.max_age_days:
            reasons.append(Reason("too_old", "block", f"{age} days; at most {rules.max_age_days}"))
    if any(m.startswith("blank:") for m in page_methods or ()):
        reasons.append(Reason("not_transcribed", "block", "a page is waiting for transcription"))
    if any(m.startswith("illegible:") for m in page_methods or ()):
        reasons.append(Reason("illegible", "block", "a page could not be read"))
    if rules.needs_period:
        start, end = _parse(manifest.get("period_start")), _parse(manifest.get("period_end"))
        if image:
            reasons.append(
                Reason("one_moment", "block", "a screenshot shows one moment, not a period")
            )
        elif start is None or end is None:
            reasons.append(Reason("no_period", "block", "period_start and period_end"))
        elif end <= start:
            reasons.append(Reason("period_inverted", "block"))
    if rules.needs_subject:
        named = manifest.get("subject_employee_id")
        if not named:
            reasons.append(Reason("no_subject", "block"))
        elif subject_entity_id is None:
            reasons.append(Reason("subject_unresolved", "block", str(named)))
    if rules.needs_issuer:
        issuer = manifest.get("issuer") or (front_matter or {}).get("approved_by")
        if not issuer:
            reasons.append(Reason("no_issuer", "warn", "say who issued or approved it"))
    if image_like and issued_at is not None:
        dates = visible_dates(text)
        if not dates:
            reasons.append(Reason("no_visible_date", "warn", "no date can be read in the image"))
        elif all(abs((d - issued_at.date()).days) > rules.visible_date_days for d in dates):
            reasons.append(
                Reason(
                    "date_mismatch",
                    "block",
                    f"visible {sorted(dates)[-1].isoformat()}, stated"
                    f" {issued_at.date().isoformat()}",
                )
            )
    if badge == "customer_attested" and image:
        reasons.append(Reason("customer_attested_image", "warn", "an upload, not a capture"))
    return Admissibility(badge, tuple(reasons))
