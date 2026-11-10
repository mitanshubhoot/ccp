"""NT5 (A-98): labelled citation cases. Zero false accepts: a spliced or paraphrased quote, a
date in the wrong order or an ambiguous form, a status the quote contradicts, a yes behind a
negation, or a subject the quote does not name never verifies."""

from __future__ import annotations

from typing import Any

import pytest
from hypothesis import given
from hypothesis import settings as hsettings
from hypothesis import strategies as st
from pydantic import BaseModel

from ccp.evaluators.citations import quote_supported, verify_fields
from ccp.evaluators.extraction import schema_for
from ccp.evaluators.extraction.base import Cited
from ccp.evaluators.extraction.gh4 import DisclosurePolicyFields
from ccp.evaluators.extraction.hr3 import TrainingRecordFields

RECORD = (
    "Security Awareness Training 2026\n"
    "This certifies that Ana Torres (employee id E1058) completed the course.\n"
    "Provider: KnowBe4\n"
    "Status: Completed\n"
    "Completion date: 2026-03-14\n"
)
IN_PROGRESS = (
    "Security Awareness Training 2026\n"
    "Learner: Ana Torres (employee id E1058)\n"
    "Status: In progress (module 3 of 5)\n"
)
POLICY = (
    "# Security Policy\n"
    "Report vulnerabilities to security@acme.example. Reports are acknowledged within 5"
    " business days.\n"
)
NO_POLICY = "We do not have a vulnerability disclosure policy yet.\nWe aim to respond promptly.\n"
SUBJECT = ("Ana Torres", "E1058")


def _hr3(chunk: str, **fields: Any) -> TrainingRecordFields:
    base: dict[str, Any] = {"subject_name_in_document": False, "completion_status": "unknown"}
    for name, (value, quote) in fields.items():
        base[name] = value
        base[f"{name}_citation"] = Cited(chunk_id="c1", quote=quote)
    return TrainingRecordFields(**base)


def _verified(parsed: BaseModel, chunk: str, schema: str) -> dict[str, Any]:
    out = verify_fields(parsed, {"c1": chunk}, schema_for(schema), subject_names=SUBJECT)
    return out.values


# (description, chunk, field, value, quote, accept)
CASES: list[tuple[str, str, str, Any, str, bool]] = [
    ("exact status", RECORD, "completion_status", "completed", "Status: Completed", True),
    (
        "status contradicted",
        IN_PROGRESS,
        "completion_status",
        "completed",
        "Status: In progress (module 3 of 5)",
        False,
    ),
    (
        "status from another line",
        RECORD,
        "completion_status",
        "completed",
        "Provider: KnowBe4",
        False,
    ),
    (
        "in progress read right",
        IN_PROGRESS,
        "completion_status",
        "in_progress",
        "Status: In progress (module 3 of 5)",
        True,
    ),
    ("exact date", RECORD, "completion_date", "2026-03-14", "Completion date: 2026-03-14", True),
    ("swapped date", RECORD, "completion_date", "2026-14-03", "Completion date: 2026-03-14", False),
    (
        "date not in quote",
        RECORD,
        "completion_date",
        "2026-03-15",
        "Completion date: 2026-03-14",
        False,
    ),
    (
        "spliced quote",
        RECORD,
        "completion_date",
        "2026-03-14",
        "Status: Completed Completion date: 2026-03-14 KnowBe4",
        False,
    ),
    (
        "paraphrased quote",
        RECORD,
        "completion_status",
        "completed",
        "Ana Torres completed the training",
        False,
    ),
    (
        "subject named",
        RECORD,
        "subject_name_in_document",
        True,
        "This certifies that Ana Torres (employee id E1058) completed the course.",
        True,
    ),
    (
        "someone else named",
        RECORD.replace("Ana Torres", "Bo Lind").replace("E1058", "E2001"),
        "subject_name_in_document",
        True,
        "This certifies that Bo Lind (employee id E2001) completed the course.",
        False,
    ),
    ("provider span", RECORD, "provider", "KnowBe4", "Provider: KnowBe4", True),
    ("provider not shown", RECORD, "provider", "Proofpoint", "Provider: KnowBe4", False),
]


@pytest.mark.parametrize(
    ("label", "chunk", "name", "value", "quote", "accept"), CASES, ids=[c[0] for c in CASES]
)
def test_training_record_cases(
    label: str, chunk: str, name: str, value: Any, quote: str, accept: bool
) -> None:
    values = _verified(_hr3(chunk, **{name: (value, quote)}), chunk, "TrainingRecordFields")
    assert (values[name] == value) is accept, label


def _gh4(**fields: Any) -> DisclosurePolicyFields:
    base: dict[str, Any] = {"policy_exists": False, "timeline_stated": False}
    for name, (value, quote) in fields.items():
        base[name] = value
        base[f"{name}_citation"] = Cited(chunk_id="c1", quote=quote)
    return DisclosurePolicyFields(**base)


@pytest.mark.parametrize(
    ("chunk", "name", "value", "quote", "accept"),
    [
        (POLICY, "policy_exists", True, "# Security Policy", True),
        (
            NO_POLICY,
            "policy_exists",
            True,
            "We do not have a vulnerability disclosure policy yet.",
            False,
        ),
        (POLICY, "timeline_stated", True, "Reports are acknowledged within 5 business days.", True),
        (NO_POLICY, "timeline_stated", True, "We aim to respond promptly.", False),
        (
            POLICY,
            "timeline_text",
            "within 5 business days",
            "Reports are acknowledged within 5 business days.",
            True,
        ),
        (
            POLICY,
            "timeline_text",
            "within 10 business days",
            "Reports are acknowledged within 5 business days.",
            False,
        ),
    ],
)
def test_disclosure_policy_cases(
    chunk: str, name: str, value: Any, quote: str, accept: bool
) -> None:
    values = _verified(_gh4(**{name: (value, quote)}), chunk, "DisclosurePolicyFields")
    assert (values[name] == value) is accept


@pytest.mark.parametrize(
    ("quote", "accept"),
    [
        ("Completion date: March 14, 2026", True),
        ("Completion date: 14 March 2026", True),
        ("Completion date: 14 Mar 2026", True),
        ("Completion date: 03/14/2026", False),  # day and month order must be guessed
        ("Completion date: 14/03/2026", False),
    ],
)
def test_only_unambiguous_date_forms_support_a_date(quote: str, accept: bool) -> None:
    chunk = f"Status: Completed\n{quote}\n"
    values = _verified(
        _hr3(chunk, completion_date=("2026-03-14", quote)), chunk, "TrainingRecordFields"
    )
    assert (values["completion_date"] == "2026-03-14") is accept


def test_invisible_characters_neither_hide_nor_forge_a_quote() -> None:
    chunk = "Status: Com​pleted\nCompletion date: 2026-03-14\n"
    assert quote_supported("Status: Completed", chunk)  # stripped, the span matches
    assert quote_supported("Status: Com⁠pleted", "Status: Completed")
    assert not quote_supported("Status: Completed today", chunk)


@hsettings(max_examples=200, deadline=None)
@given(data=st.data())
def test_a_splice_of_two_separate_spans_is_never_found(data: st.DataObject) -> None:
    words = RECORD.split()
    i = data.draw(st.integers(0, len(words) - 4))
    j = data.draw(st.integers(i + 3, len(words) - 1))  # a gap of at least two words
    left = " ".join(words[i : i + 2])
    right = words[j]
    spliced = f"{left} {right}"
    contiguous = " ".join(RECORD.split())
    if spliced.lower() not in contiguous.lower():
        assert not quote_supported(spliced, RECORD)
