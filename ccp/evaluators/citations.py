"""Post-generation citation and support verification (spec.md S3, Layer 7 step 5, A-98).

    for each cited field
      chunk id must be one the model was shown          else citation_unknown_chunk
      quote must be a contiguous run of the chunk's words else citation_quote_not_found
        (case, spacing and punctuation aside; no word-overlap fallback)
      the value must be shown by its quote              else citation_unsupported_value
        literal fields (dates, names, text): the value itself, as one span; a date also in
          an unambiguous spelled-out form, never a day/month order the reader must guess
        status and yes/no fields: the words that say so, none that contradict it, and no
          negation behind a yes
    a field that fails is set to None; the rule decides with it missing.
A positive claim without any citation is dropped too; absence (False, None, "unknown")
needs no citation. Every cited field with a non-absent value has a check: a field this
module has no check for is dropped with `no_value_check`, never passed through.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from pydantic import BaseModel

from ccp.evaluators.extraction import SchemaMeta
from ccp.evaluators.verdict import Citation
from ccp.security.prompt_guard import INVISIBLE

MAX_QUOTE_CHARS = 300
_WS = re.compile(r"\s+")
_WORD = re.compile(r"[^\W_]+")
_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
ABSENT_VALUES: tuple[Any, ...] = (None, False, "unknown", "not_started", "")
NEGATIONS = re.compile(r"\b(?:no|not|never|none|without|neither|nor|cannot)\b|n't\b")


def normalize(text: str) -> str:
    return _WS.sub(" ", INVISIBLE.sub("", text)).strip().lower()


def _words(text: str) -> str:
    return " ".join(_WORD.findall(normalize(text)))


def quote_supported(quote: str, chunk_text: str) -> bool:
    """The quote's words are one contiguous run of the chunk's words, in order. Punctuation
    and spacing may differ (a dropped full stop); a quote spliced from two places, reordered,
    or paraphrased is not found, however many of its words the chunk contains."""
    q = _words(quote)
    return bool(q) and f" {q} " in f" {_words(chunk_text)} "


@dataclass(frozen=True)
class Support:
    """How a quote must show a value that is not a literal span of it."""

    any_of: tuple[str, ...] = ()  # at least one must appear
    none_of: tuple[str, ...] = ()  # none may appear
    pattern: str | None = None  # a regex that must match instead of any_of
    subject: bool = False  # the quote must name the subject being evaluated
    allow_negation: bool = False


_NOT_DONE = (
    "in progress",
    "in-progress",
    "not started",
    "not completed",
    "not complete",
    "incomplete",
    "pending",
    "overdue",
    "not yet",
    "expired",
    "failed",
)
VALUE_SUPPORT: dict[str, dict[Any, Support]] = {
    "completion_status": {
        "completed": Support(
            any_of=("completed", "complete", "passed", "finished"), none_of=_NOT_DONE
        ),
        "in_progress": Support(
            any_of=("in progress", "in-progress", "started", "pending", "incomplete"),
            allow_negation=True,
        ),
    },
    "timeline_stated": {
        True: Support(pattern=r"\b\d+\s*(?:business |working |calendar )?(?:day|hour|week)s?\b")
    },
    "policy_exists": {True: Support()},
    "subject_name_in_document": {True: Support(subject=True)},
}

_MONTHS = (
    "january",
    "february",
    "march",
    "april",
    "may",
    "june",
    "july",
    "august",
    "september",
    "october",
    "november",
    "december",
)


def _date_forms(value: str) -> tuple[str, ...]:
    """An ISO date and the spelled-out forms that cannot be misread; never 03/04/2026."""
    if not _ISO_DATE.match(value):
        return (value,)
    try:
        d = date.fromisoformat(value)
    except ValueError:
        return (value,)
    month = _MONTHS[d.month - 1]
    short = month[:3]
    return (
        value,
        f"{month} {d.day}, {d.year}",
        f"{month} {d.day} {d.year}",
        f"{d.day} {month} {d.year}",
        f"{short} {d.day}, {d.year}",
        f"{d.day} {short} {d.year}",
    )


def _contains(text: str, phrase: str) -> bool:
    return re.search(rf"(?<![a-z0-9]){re.escape(phrase)}(?![a-z0-9])", text) is not None


def value_in_quote(value: Any, quote: str) -> bool:
    """A literal value appears in its quote as one span (a date in an unambiguous form)."""
    if value in ABSENT_VALUES:
        return True
    if isinstance(value, bool):
        return False  # a yes/no is never a literal span; it needs a Support rule
    q = normalize(quote)
    return any(_contains(q, normalize(form)) for form in _date_forms(str(value)))


def value_supported(
    name: str, value: Any, quote: str, subject_names: tuple[str, ...] = ()
) -> bool | None:
    """True or False under the field's Support rule; None when the field has no rule."""
    rules = VALUE_SUPPORT.get(name)
    if rules is None or value not in rules:
        return None
    rule = rules[value]
    q = normalize(quote)
    if not rule.allow_negation and NEGATIONS.search(q):
        return False
    if any(_contains(q, phrase) for phrase in rule.none_of):
        return False
    if rule.pattern is not None and not re.search(rule.pattern, q):
        return False
    if rule.any_of and not any(_contains(q, phrase) for phrase in rule.any_of):
        return False
    if rule.subject:
        names = [normalize(n) for n in subject_names if n and n.strip()]
        if not names or not any(_contains(q, n) for n in names):
            return False
    return True


def check_value(
    name: str, value: Any, quote: str, meta: SchemaMeta, subject_names: tuple[str, ...] = ()
) -> str | None:
    """None when the quote shows the value; otherwise the reason it does not."""
    if value in ABSENT_VALUES:
        return None
    supported = value_supported(name, value, quote, subject_names)
    if supported is not None:
        return None if supported else "citation_unsupported_value"
    if name in meta.value_in_quote:
        return None if value_in_quote(value, quote) else "citation_unsupported_value"
    return "no_value_check"


@dataclass
class VerifiedFields:
    values: dict[str, Any]
    citations: list[Citation] = field(default_factory=list)
    events: list[str] = field(default_factory=list)
    required_total: int = 0
    required_covered: int = 0
    citations_given: int = 0
    citations_verified: int = 0

    @property
    def coverage(self) -> float:
        return self.required_covered / self.required_total if self.required_total else 1.0

    @property
    def citation_rate(self) -> float:
        return self.citations_verified / self.citations_given if self.citations_given else 1.0

    @property
    def lost_required_citation(self) -> bool:
        return any(e.split(":")[1] in self.required_names for e in self.events if ":" in e)

    required_names: tuple[str, ...] = ()


def verify_fields(
    parsed: BaseModel,
    chunks: dict[str, str],
    meta: SchemaMeta,
    subject_names: tuple[str, ...] = (),
) -> VerifiedFields:
    values: dict[str, Any] = {}
    out = VerifiedFields(
        values=values, required_total=len(meta.required), required_names=meta.required
    )
    for name in meta.cited:
        value = getattr(parsed, name, None)
        cited = getattr(parsed, f"{name}_citation", None)
        if cited is None:
            if value in ABSENT_VALUES:
                values[name] = value
                if name in meta.required:
                    out.required_covered += 1
            else:
                values[name] = None
                out.events.append(f"uncited_field:{name}")
            continue
        out.citations_given += 1
        chunk_id, quote = str(cited.chunk_id), str(cited.quote)[:MAX_QUOTE_CHARS]
        chunk_text = chunks.get(chunk_id)
        verified = False
        if chunk_text is None:
            out.events.append(f"citation_unknown_chunk:{name}:{chunk_id}")
        elif not quote_supported(quote, chunk_text):
            out.events.append(f"citation_quote_not_found:{name}:{chunk_id}")
        else:
            problem = check_value(name, value, quote, meta, subject_names)
            if problem is None:
                verified = True
            else:
                out.events.append(f"{problem}:{name}:{chunk_id}")
        out.citations.append(
            Citation(chunk_id=chunk_id, quote=quote, supports=name, verified=verified)
        )
        if verified:
            out.citations_verified += 1
            values[name] = value
            if name in meta.required:
                out.required_covered += 1
        else:
            values[name] = None
    for name, value in parsed.model_dump().items():  # uncited scalar fields pass through
        if name not in values and not name.endswith("_citation"):
            values[name] = value
    return out
