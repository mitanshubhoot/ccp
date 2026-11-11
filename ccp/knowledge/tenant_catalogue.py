"""Tenant custom controls on top of the common catalogue (spec.md A-166).

A tenant's own controls, tests and declarative checks live in
`ccp/knowledge/tenants/<tenant>/` in the same format as the common catalogue: a
`controls.yaml`, a `tests.yaml` and a `checks/` folder, each optional. They are read on
top of the common files and checked by the same validator, so a tenant control can map to
the common frameworks and point at common or tenant tests, and a tenant test can be
evaluated by a tenant check. The tenant's catalogue version covers the common version
and its own files, so a verdict under a tenant catalogue names exactly what it ran under.
A tenant check goes through the same drafting gates and review as a common one (A-165).
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import yaml

from ccp.knowledge import catalogue
from ccp.knowledge.graph import KNOWLEDGE_DIR, Control, KnowledgeGraph, TestSpec
from ccp.knowledge.graph import load as load_common

TENANTS_DIR = "tenants"


def overlay_dir(tenant_id: str, directory: Path = KNOWLEDGE_DIR) -> Path:
    return directory / TENANTS_DIR / tenant_id


def files(tenant_id: str, directory: Path = KNOWLEDGE_DIR) -> list[Path]:
    root = overlay_dir(tenant_id, directory)
    found = [root / "controls.yaml", root / "tests.yaml", *sorted((root / "checks").glob("*.yaml"))]
    return [p for p in found if p.is_file()]


def load_for(tenant_id: str, directory: Path = KNOWLEDGE_DIR) -> KnowledgeGraph:
    """The common graph with the tenant's controls and tests added; a tenant entry may not
    replace a common one."""
    common = load_common(directory)
    root = overlay_dir(tenant_id, directory)
    controls = dict(common.controls)
    tests = dict(common.tests)
    if (root / "tests.yaml").is_file():
        for raw in (yaml.safe_load((root / "tests.yaml").read_text(encoding="utf-8")) or {}).get(
            "tests", []
        ):
            spec = TestSpec.model_validate(raw)
            if spec.test_id in tests:
                raise ValueError(f"tenant test {spec.test_id} would replace a common test")
            tests[spec.test_id] = spec
    if (root / "controls.yaml").is_file():
        for raw in (yaml.safe_load((root / "controls.yaml").read_text(encoding="utf-8")) or {}).get(
            "controls", []
        ):
            control = Control.model_validate(raw)
            if control.control_id in controls:
                raise ValueError(
                    f"tenant control {control.control_id} would replace a common control"
                )
            controls[control.control_id] = control
    return KnowledgeGraph(frameworks=common.frameworks, controls=controls, tests=tests)


def version(tenant_id: str, directory: Path = KNOWLEDGE_DIR) -> str:
    """The common catalogue's version, or with the tenant's own files a version over both."""
    own = files(tenant_id, directory)
    base = catalogue.version(directory)
    if not own:
        return base
    digest = hashlib.sha256(base.encode())
    for path in own:
        digest.update(path.relative_to(directory).as_posix().encode())
        digest.update(path.read_bytes().replace(b"\r\n", b"\n"))
    return digest.hexdigest()[:12]
