"""The fix catalogue (spec.md A-159): versioned data, like the action catalogue.

Each fix names the tests whose failing resources it fixes, its parameters with the pattern
each must match whole, and its templates (ccp/knowledge/fixes.yaml). This module loads it
and checks a choice of fix and parameters, whoever made it; ccp/remediation/fixes.py
renders a checked choice. A model's choice is bounded here inside its graph (A-160).
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

import yaml

from ccp.knowledge.graph import KNOWLEDGE_DIR

FIXES_FILE = KNOWLEDGE_DIR / "fixes.yaml"
TerraformKind = Literal["add_block", "set_attribute", "remove_block"]
_PLACEHOLDER = re.compile(r"\{([a-z_]+)\}")


class Refused(ValueError):
    """A fix choice that cannot be rendered: an unknown fix or a parameter out of bounds."""


@dataclass(frozen=True)
class TerraformSpec:
    kind: TerraformKind
    on: str
    template: str | None = None
    attribute: str | None = None
    value: str | None = None
    types: tuple[str, ...] = ()


@dataclass(frozen=True)
class Fix:
    fix_id: str
    version: int
    title: str
    tests: tuple[str, ...]
    params: dict[str, str]  # name -> pattern
    terraform: TerraformSpec | None
    cli: tuple[str, ...]
    console: tuple[str, ...]
    verify: str
    sha256: str  # of the fix's own entry: the templates a suggestion was rendered from

    @property
    def key_param(self) -> str:
        """The parameter that names the resource: the first one declared."""
        return next(iter(self.params))


def _fix(fix_id: str, raw: dict[str, Any]) -> Fix:
    unknown = set(raw) - {
        "version",
        "title",
        "tests",
        "params",
        "terraform",
        "cli",
        "console",
        "verify",
    }
    if unknown:
        raise ValueError(f"fixes.yaml {fix_id}: unknown keys {sorted(unknown)}")
    params = {str(k): str(v) for k, v in (raw.get("params") or {}).items()}
    if not params:
        raise ValueError(f"fixes.yaml {fix_id}: a fix names the resource it acts on")
    for name in params:
        re.compile(params[name])
    tf = raw.get("terraform")
    spec: TerraformSpec | None = None
    if isinstance(tf, dict):
        kind = tf.get("kind")
        if kind not in ("add_block", "set_attribute", "remove_block"):
            raise ValueError(f"fixes.yaml {fix_id}: terraform.kind is {kind!r}")
        spec = TerraformSpec(
            kind,
            str(tf["resource"]),
            tf.get("template"),
            tf.get("attribute"),
            None if tf.get("value") is None else str(tf.get("value")),
            tuple(str(t) for t in tf.get("types") or ()),
        )
        if kind == "add_block" and not spec.template:
            raise ValueError(f"fixes.yaml {fix_id}: add_block needs a template")
        if kind == "set_attribute" and not (spec.attribute and spec.value is not None):
            raise ValueError(f"fixes.yaml {fix_id}: set_attribute needs attribute and value")
        if kind == "remove_block" and not spec.types:
            raise ValueError(f"fixes.yaml {fix_id}: remove_block names the types it removes")
    for line in [*(raw.get("cli") or []), *(raw.get("console") or [])]:
        for name in _PLACEHOLDER.findall(str(line)):
            if name not in params:
                raise ValueError(f"fixes.yaml {fix_id}: {{{name}}} is not a parameter")
    canonical = json.dumps(raw, sort_keys=True, default=str).encode()
    return Fix(
        fix_id,
        int(raw.get("version") or 1),
        str(raw.get("title") or fix_id),
        tuple(str(t) for t in raw.get("tests") or ()),
        params,
        spec,
        tuple(str(c) for c in raw.get("cli") or ()),
        tuple(str(c) for c in raw.get("console") or ()),
        str(raw.get("verify") or ""),
        hashlib.sha256(canonical).hexdigest(),
    )


@lru_cache(maxsize=2)
def load(path: Path = FIXES_FILE) -> dict[str, Fix]:
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return {str(k): _fix(str(k), v) for k, v in raw.items()}


def for_test(test_id: str) -> list[Fix]:
    return [f for f in load().values() if test_id in f.tests]


def get(fix_id: str) -> Fix:
    try:
        return load()[fix_id]
    except KeyError:
        raise Refused(f"no fix {fix_id!r} in the catalogue") from None


def check_params(fix: Fix, params: dict[str, Any]) -> dict[str, str]:
    """Exactly the declared parameters, each a string matching its pattern whole."""
    given = set(params)
    if given != set(fix.params):
        raise Refused(f"{fix.fix_id} takes {sorted(fix.params)}, got {sorted(given)}")
    out: dict[str, str] = {}
    for name, pattern in fix.params.items():
        value = params[name]
        if not isinstance(value, str) or re.fullmatch(pattern, value) is None:
            raise Refused(f"{fix.fix_id}: {name}={value!r} does not match {pattern}")
        out[name] = value
    return out
