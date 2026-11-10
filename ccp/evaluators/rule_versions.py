"""Rule versions (spec.md A-101): every verdict names the rule that decided it, and the
evaluation key includes that version and a hash of the parameters it ran with. A corrected
rule, or changed tenant parameters, over unchanged data is therefore a new evaluation rather
than a duplicate that is silently dropped.

    rule version   sha256 of the source that decides the test: its deterministic module (and
                   the shared helpers) or its interpretive rule (and the rule registry)
    parameters     sha256 of the test's parameters and the tenant settings rules read
    stamp          "<rule version>:<parameters>", stored on the verdict as rule_version
"""

from __future__ import annotations

import hashlib
import json
from functools import lru_cache
from pathlib import Path

from ccp.config import TenantSettings
from ccp.knowledge.graph import TestSpec

_EVALUATORS = Path(__file__).resolve().parent
_SHARED = {
    "deterministic": ("common.py",),
    "rules": ("__init__.py",),
}
# Tenant settings a rule reads; display and budget settings do not decide a status.
RULE_SETTINGS = (
    "github_org",
    "org_admin_limit",
    "critical_asset_tags",
    "termination_revocation_sla_hours",
)


def rule_files(test_id: str) -> tuple[Path, ...]:
    """The source files that decide ``test_id``: its module and the helpers it shares."""
    module = test_id.lower().replace("-", "") + ".py"
    for package in ("deterministic", "rules"):
        path = _EVALUATORS / package / module
        if path.is_file():
            shared = tuple(_EVALUATORS / package / name for name in _SHARED[package])
            return (path, *shared)
    raise LookupError(f"no rule module for {test_id!r} under ccp/evaluators")


@lru_cache(maxsize=64)
def rule_version(test_id: str) -> str:
    digest = hashlib.sha256()
    for path in rule_files(test_id):
        digest.update(path.name.encode())
        digest.update(path.read_bytes().replace(b"\r\n", b"\n"))
    return digest.hexdigest()[:12]


def parameters_hash(spec: TestSpec, tenant: TenantSettings) -> str:
    material = {
        "parameters": dict(spec.parameters),
        "evidence_max_age_days": spec.evidence_max_age_days,
        "tenant": {name: getattr(tenant, name) for name in RULE_SETTINGS},
    }
    canonical = json.dumps(material, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()[:12]


def rule_stamp(spec: TestSpec, tenant: TenantSettings) -> str:
    return f"{rule_version(spec.test_id)}:{parameters_hash(spec, tenant)}"
