"""Release bundles (spec.md A-142): one id for everything that shapes an AI result.

    models            the model id of every role: tier 2, tier 3, the agents, the judge, the
                      second reader, the embedder
    model behaviour   for each model, how thinking is turned off and whether effort applies
                      (ccp/llm/models.py), so a change of setting is a new bundle
    prompts, tools    the agent bundle hash (A-100): prompt files, every tool's declaration,
                      the tool error texts, allowlists and the tool policy
    rules             the rule catalogue's version (A-128)
    configuration     digests of the retrieval and evaluation configuration

The id is the first 16 hex digits of the manifest's digest. Every verdict and every agent run
records the id it ran under, and `release_bundles` keeps each manifest once, so any result
can be traced to exactly what produced it and a change of model, prompt or setting is a new
bundle that the eval gates must pass (A-144) and that re-earns autonomy (A-100).

The agent bundle is read through a dynamic import: it describes the agent layer's tools,
which sits above this one, and every bundle must include it whichever layer asks.
"""

from __future__ import annotations

import hashlib
import importlib
import json
from functools import lru_cache
from pathlib import Path
from typing import Any

from ccp.config import REPO_ROOT, Settings
from ccp.knowledge import catalogue
from ccp.llm.models import MODELS

CONFIG_FILES = (
    "retrieval.yaml",
    "evaluation.yaml",
    "eval_gates.yaml",
    "task_kinds.yaml",  # A-151
    "context_ceilings.yaml",  # A-153
)
_MANIFESTS: dict[str, dict[str, Any]] = {}


def _digest(path: Path) -> str | None:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None


def _agent_bundle() -> str:
    module = importlib.import_module("ccp.agent.bundle")
    value: str = module.bundle_sha256()
    return value


def roles(settings: Settings) -> dict[str, str]:
    return {
        "tier2": settings.ccp_tier2_model,
        "tier3": settings.ccp_tier3_model,
        "investigator": settings.investigator_model,
        "policy": settings.policy_model,
        "copilot": settings.copilot_model,
        "task_item": settings.task_model,
        "judge": settings.ccp_judge_model,
        "second_reader": settings.ccp_second_reader_model,
        "adequacy": settings.ccp_adequacy_model,  # A-162
        "questionnaire": settings.ccp_questionnaire_model,  # A-163
        "embed": settings.ccp_embed_model,
    }


def manifest(settings: Settings) -> dict[str, Any]:
    models = roles(settings)
    behaviour = {
        model: {"thinking_off": spec.off, "effort": spec.effort}
        for model in sorted(set(models.values()))
        if (spec := MODELS.get(model)) is not None
    }
    return {
        "models": models,
        "model_behaviour": behaviour,
        "prompts_and_tools": _agent_bundle(),
        "rules": catalogue.version(),
        "config": {name: _digest(REPO_ROOT / "config" / name) for name in CONFIG_FILES},
    }


@lru_cache(maxsize=32)
def _id_for(material: str) -> str:
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]


def bundle_id(settings: Settings) -> str:
    """The bundle these settings run under. The files it reads do not change while a process
    runs, so the id is kept per set of models."""
    models = tuple(sorted(roles(settings).items()))
    identifier = _BY_MODELS.get(models)
    if identifier is None:
        found = manifest(settings)
        material = json.dumps(found, sort_keys=True, separators=(",", ":"))
        identifier = _id_for(material)
        _MANIFESTS.setdefault(identifier, found)
        _BY_MODELS[models] = identifier
    return identifier


_BY_MODELS: dict[tuple[tuple[str, str], ...], str] = {}


def register(cur: Any, identifier: str) -> None:
    """Keep the bundle's manifest, once, inside the caller's transaction."""
    found = _MANIFESTS.get(identifier)
    if found is None:
        return
    cur.execute(
        "INSERT INTO release_bundles (bundle_id, manifest) VALUES (%s, %s)"
        " ON CONFLICT (bundle_id) DO NOTHING",
        (identifier, json.dumps(found, sort_keys=True)),
    )


def manifest_of(identifier: str) -> dict[str, Any] | None:
    """A manifest this process computed (the database holds every one: release_bundles)."""
    return _MANIFESTS.get(identifier)


def differences(before: dict[str, Any], after: dict[str, Any], prefix: str = "") -> list[str]:
    """What changed between two manifests, as dotted paths."""
    out: list[str] = []
    for key in sorted(set(before) | set(after)):
        a, b = before.get(key), after.get(key)
        path = f"{prefix}{key}"
        if isinstance(a, dict) and isinstance(b, dict):
            out.extend(differences(a, b, f"{path}."))
        elif a != b:
            out.append(f"{path}: {a!r} -> {b!r}")
    return out
