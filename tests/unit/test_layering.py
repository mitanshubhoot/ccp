"""Layering and import rules from spec.md section 2, enforced as tests.

1. The import-linter contract in pyproject.toml: no layer imports a layer above it.
2. String-level rules that import-linter cannot express:
   - the Anthropic SDK is imported only in ccp/llm/anthropic_client.py
   - DATABASE_ADMIN_URL is referenced only by config, the migration runner, and tests
   - deterministic evaluators never import a model client
   - agent tools never import anything with a side effect
Rules over packages that do not exist yet pass vacuously and start biting the phase
that creates the package.
"""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
PKG = REPO_ROOT / "ccp"


def _python_files() -> list[Path]:
    return sorted(PKG.rglob("*.py"))


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


def _rel(path: Path) -> str:
    return path.relative_to(REPO_ROOT).as_posix()


def test_import_linter_contract_holds() -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from importlinter.cli import lint_imports; raise SystemExit(lint_imports())",
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_anthropic_sdk_imported_only_by_the_provider_module() -> None:
    offenders = [
        _rel(p)
        for p in _python_files()
        if any(name == "anthropic" or name.startswith("anthropic.") for name in _imports(p))
        and _rel(p) != "ccp/llm/anthropic_client.py"
    ]
    assert offenders == [], f"anthropic imported outside the provider seam: {offenders}"


def test_admin_url_referenced_only_by_config_and_migration_runner() -> None:
    allowed = {"ccp/config.py", "ccp/storage/db.py"}
    offenders = [
        _rel(p)
        for p in _python_files()
        if "database_admin_url" in p.read_text(encoding="utf-8").lower() and _rel(p) not in allowed
    ]
    assert offenders == [], f"admin URL referenced outside the migration runner: {offenders}"


def test_deterministic_evaluators_never_import_a_model_client() -> None:
    forbidden = ("ccp.llm", "anthropic", "langgraph", "httpx", "voyageai", "openai")
    offenders = [
        _rel(p)
        for p in _python_files()
        if "ccp/evaluators/deterministic/" in _rel(p)
        and any(name == f or name.startswith(f + ".") for name in _imports(p) for f in forbidden)
    ]
    assert offenders == [], f"deterministic evaluator imports a model client: {offenders}"


def test_agent_tools_import_nothing_with_side_effects() -> None:
    tools = PKG / "agent" / "tools.py"
    if not tools.exists():
        return
    forbidden = ("ccp.remediation", "ccp.approvals", "ccp.policy", "ccp.investigation")
    bad = [name for name in _imports(tools) if any(name.startswith(f) for f in forbidden)]
    assert bad == [], f"agent tools import side-effecting modules: {bad}"
