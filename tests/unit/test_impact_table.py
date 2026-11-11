"""MT8 (A-126): the impact table is data, checked against a closed vocabulary at load.

The defect: the impact table was code (a chain of conditions in ccp/evaluators/impact.py), so
what a change re-evaluates could not be read, reviewed or versioned beside the tests it
names, and a new check had to be wired into that code to ever run on the change path.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ccp.evaluators import impact

# The reach the code table had before it became data: the data must reproduce it exactly.
BEFORE = {
    "persons": {"HR-1", "GH-1"},
    "person_links": {"HR-1", "GH-1"},
    "access_grants": {"HR-1", "GH-1", "GH-2"},
    "assets": {"GH-2", "GH-3", "HR-4"},
    "evidence": {"GH-4", "GH-5", "HR-2", "HR-3", "HR-4", "HR-5"},
}
# What later steps added to the table, as data from the start (A-140).
ADDED = {"access_grants": {"AWS-2"}, "assets": {"AWS-1", "AWS-3"}}
TESTS = ("HR-1", "GH-1", "GH-2", "GH-3", "GH-4", "GH-5", "HR-2", "HR-3", "HR-4", "HR-5")


def test_the_data_reproduces_the_code_tables_reach() -> None:
    expected = {table: BEFORE[table] | ADDED.get(table, set()) for table in BEFORE}
    assert {table: set(tests) for table, tests in impact.IMPACT_REACH.items()} == expected


def _load(tmp_path: Path, rule: str) -> tuple[impact.ImpactRule, ...]:
    path = tmp_path / "impact.yaml"
    path.write_text("rules:\n" + rule, encoding="utf-8")
    return impact.load_table(path, TESTS)


def test_a_well_formed_rule_loads(tmp_path: Path) -> None:
    (rule,) = _load(
        tmp_path,
        "  - {table: evidence, when: {evidence_type: [account_registry]}, tests: [HR-5],"
        " subject: tenant}\n",
    )
    assert rule.tests == ("HR-5",) and rule.when == {"evidence_type": ["account_registry"]}


@pytest.mark.parametrize(
    ("rule", "message"),
    [
        (
            "{table: evidence, when: {owner_left: true}, tests: [HR-5], subject: tenant}",
            "unknown condition",
        ),
        (
            "{table: persons, when: {evidence_type: [hr_roster]}, tests: [HR-1], subject: person}",
            "unknown condition",  # a condition of another table
        ),
        (
            "{table: evidence, when: {evidence_type: account_registry}, tests: [HR-5],"
            " subject: tenant}",
            "valid list",  # a single value where a list belongs fails the type check
        ),
        (
            "{table: assets, when: {tagged: false}, tests: [HR-4], subject: tenant}",
            "takes true",
        ),
        (
            "{table: assets, when: {tagged: true}, tests: [HR-4], subject: person}",
            "unknown subject",
        ),
        (
            "{table: assets, when: {tagged: true}, tests: [XX-9], subject: tenant}",
            "unknown tests",
        ),
        (
            "{table: chunks, when: {}, tests: [HR-4], subject: tenant}",
            "table",
        ),
    ],
)
def test_a_rule_outside_the_vocabulary_fails_to_load(
    tmp_path: Path, rule: str, message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        _load(tmp_path, f"  - {rule}\n")
