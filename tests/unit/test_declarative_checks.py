"""MT8 (A-127): long-tail checks as YAML, read by a small, total interpreter.

The defect: every tier-1 check was Python, so each long-tail check (the kind compliance
platforms keep hundreds of) meant new code to review, and nothing held a check to deciding
its status and nothing else. HR-2 is now a YAML check and decides exactly as its Python rule
did; the fixture tenants' tabled results pin that end to end.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import yaml

from ccp.config import Settings
from ccp.evaluators import rule_versions
from ccp.evaluators.context import EvaluationContext
from ccp.evaluators.deterministic import declarative
from ccp.evaluators.verdict import Verdict
from ccp.knowledge import graph as knowledge
from ccp.normalize.entities import AccessGrant
from ccp.normalize.hris_mapper import IDP_ASSET_ID
from ccp.tenancy import TenantContext, TenantId
from ccp.types import GrantState, VerdictStatus

NOW = datetime(2026, 11, 10, 12, tzinfo=UTC)


def _identity(
    n: int, kind: str = "human", mfa: Any = True, state: GrantState = GrantState.active
) -> AccessGrant:
    return AccessGrant(
        tenant_id=TenantId("acme"),
        entity_id=f"hris:grant:id-{n}",
        observed_at=NOW - timedelta(hours=n),
        source_connector="hris",
        source_event_id=f"e{n}",
        person_entity_id=f"hris:person:{n}",
        asset_entity_id=IDP_ASSET_ID,
        role="user",
        state=state,
        state_effective_at=NOW,
        attributes={"kind": kind, "mfa_enabled": mfa, "email": f"p{n}@acme.example"},
    )


def _hr2(settings: Settings, grants: list[AccessGrant]) -> Verdict:
    ctx = EvaluationContext(
        tenant=TenantContext.for_id(settings, "acme"),
        settings=settings,
        spec=knowledge.load().tests["HR-2"],
        trigger_observed_at=NOW,
        now=NOW,
        persons={},
        grants=grants,
        assets={},
        evidence=[],
        canonical={},
        pending_review=set(),
    )
    return declarative.evaluate(ctx, declarative.load_all()["HR-2"], "tenant")


def test_hr2_as_yaml_decides_as_its_python_rule_did(base_settings: Settings) -> None:
    passed = _hr2(base_settings, [_identity(1), _identity(2)])
    assert passed.status is VerdictStatus.passed
    assert passed.reasoning == "all 2 active human identities have MFA enabled"
    failed = _hr2(
        base_settings, [_identity(1), _identity(2, mfa=False), _identity(3, "service", False)]
    )
    assert failed.status is VerdictStatus.failed
    assert failed.reasoning == "1 of 2 active human identities lack MFA: p2@acme.example"
    assert failed.evidence_refs == ("hris:grant:id-1", "hris:grant:id-2", "hris:grant:id-3")
    assert failed.valid_from == NOW - timedelta(hours=1)  # the newest identity observed
    assert _hr2(base_settings, [_identity(1, mfa=None)]).status is VerdictStatus.failed
    nothing = _hr2(base_settings, [_identity(1, state=GrantState.revoked)])
    assert (nothing.status, nothing.reasoning) == (
        VerdictStatus.needs_review,
        "no identity observations",
    )


def test_a_check_is_versioned_by_its_file_and_the_interpreter() -> None:
    files = rule_versions.rule_files("HR-2")
    assert [p.name for p in files] == ["hr2.yaml", "declarative.py", "common.py"]


def _base() -> dict[str, Any]:
    return yaml.safe_load((declarative.CHECKS_DIR / "hr2.yaml").read_text(encoding="utf-8"))


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"read": {"from": "grants", "where": [{"field": "colour", "equals": "red"}]}}, "no field"),
        (
            {"require": [{"field": "state", "equals": "active", "truthy": True}]},
            "one test",
        ),
        ({"require": [{"field": "state", "eval": "__import__('os')"}]}, "Extra inputs"),
        ({"fail": "{failing} and {secret}"}, "only"),
        ({"read": {"from": "chunks"}}, "from"),
        ({"subject": "repo"}, "subject"),
    ],
)
def test_a_check_outside_the_format_fails_to_load(
    tmp_path: Path, change: dict[str, Any], message: str
) -> None:
    (tmp_path / "hr2.yaml").write_text(yaml.safe_dump({**_base(), **change}), encoding="utf-8")
    with pytest.raises(ValueError, match=message):
        declarative.load_all(tmp_path)


def test_a_check_lives_in_the_file_named_after_its_test(tmp_path: Path) -> None:
    (tmp_path / "hr9.yaml").write_text(yaml.safe_dump(_base()), encoding="utf-8")
    with pytest.raises(ValueError, match="named after its test"):
        declarative.load_all(tmp_path)
