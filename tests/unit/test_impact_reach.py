"""NT6 (A-101): every test has a trigger on the change path, and the rule that decides a
verdict is part of its identity.

- The impact reach covers every test in the knowledge graph; a new test with no trigger
  fails here, in CI, before it can sit unevaluated.
- A rule's version is the hash of the code that decides it, and the evaluation key changes
  with the rule or the tenant parameters it reads, never with display-only settings.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from ccp.config import Settings
from ccp.evaluators import rule_versions
from ccp.evaluators.impact import IMPACT_REACH
from ccp.evaluators.verdict import evaluation_key
from ccp.knowledge import graph as knowledge

NOW = datetime(2026, 9, 11, 12, tzinfo=UTC)


def test_every_test_has_a_trigger() -> None:
    reachable = set().union(*IMPACT_REACH.values())
    tests = set(knowledge.load().tests)
    assert tests - reachable == set(), f"tests no change can trigger: {sorted(tests - reachable)}"
    assert reachable <= tests  # and nothing triggers a test that does not exist


@pytest.mark.parametrize("test_id", sorted(knowledge.load().tests))
def test_every_test_has_a_rule_version(test_id: str) -> None:
    files = rule_versions.rule_files(test_id)
    assert files and all(path.is_file() for path in files)
    version = rule_versions.rule_version(test_id)
    assert len(version) == 12 and version == rule_versions.rule_version(test_id)


def test_the_key_moves_with_the_rule_and_its_parameters(base_settings: Settings) -> None:
    spec = knowledge.load().tests["HR-1"]
    tenant = base_settings.load_tenants()["acme"]
    stamp = rule_versions.rule_stamp(spec, tenant)

    def key(rule: str) -> str:
        return evaluation_key("acme", "HR-1", "HR-1|x", NOW, None, None, rule=rule)

    longer_sla = tenant.model_copy(
        update={"termination_revocation_sla_hours": tenant.termination_revocation_sla_hours + 1}
    )
    renamed = tenant.model_copy(update={"display_name": "Acme, renamed"})
    assert rule_versions.rule_stamp(spec, longer_sla) != stamp  # a parameter the rule reads
    assert rule_versions.rule_stamp(spec, renamed) == stamp  # a display-only setting
    corrected = f"{'0' * 12}:{stamp.split(':')[1]}"  # the same data under a corrected rule
    assert key(stamp) != key(corrected) and key(stamp) == key(stamp)
    with pytest.raises(LookupError):
        rule_versions.rule_files("XX-9")
