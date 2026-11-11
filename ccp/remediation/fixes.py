"""Fix suggestions rendered by code from versioned templates (spec.md A-159).

CCP suggests; a person applies. A fix is chosen (by code from a failing verdict, or by a
model from the catalogue, A-160) as a fix id and its parameters. Nothing else reaches a
template: every parameter must match its declared pattern whole, and placeholders are filled
by code. The rendered suggestion is then checked offline before anyone sees it:

    params        exactly the declared parameters, each matching its pattern
    cli           each command starts with the template's own program and subcommand, and
                  carries no shell control character
    syntax        the changed Terraform file still closes every brace, string and heredoc
    scope         only the declared kinds of block were added, removed or changed, and only
                  around the resource the finding is about; nothing outside a block moved
    placeholders  no placeholder is left in the rendered text

A suggestion that fails a check is not offered; the failed check is recorded instead.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import re
import shlex
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

import yaml

from ccp.knowledge.graph import KNOWLEDGE_DIR
from ccp.lookups import terraform

FIXES_FILE = KNOWLEDGE_DIR / "fixes.yaml"
TerraformKind = Literal["add_block", "set_attribute", "remove_block"]
_PLACEHOLDER = re.compile(r"\{([a-z_]+)\}")
_SHELL_CONTROL = re.compile(r"[;&|`$<>\n\r\\]")
_TEMPLATE_TYPES = re.compile(r'^\s*resource\s+"([A-Za-z0-9_]+)"', re.MULTILINE)


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


# -- rendering -------------------------------------------------------------------------------


@dataclass(frozen=True)
class Check:
    name: str
    passed: bool
    detail: str = ""


@dataclass(frozen=True)
class Rendered:
    fix: Fix
    params: dict[str, str]
    terraform: dict[str, Any] | None
    cli: tuple[str, ...]
    console: tuple[str, ...]
    checks: tuple[Check, ...] = field(default=())

    @property
    def passed(self) -> bool:
        return all(c.passed for c in self.checks)

    @property
    def failed(self) -> list[Check]:
        return [c for c in self.checks if not c.passed]

    def body(self) -> dict[str, Any]:
        """What the suggestion shows, and what its payload hash covers."""
        return {
            "fix": self.fix.fix_id,
            "version": self.fix.version,
            "template_sha256": self.fix.sha256,
            "title": self.fix.title,
            "params": self.params,
            "terraform": self.terraform,
            "cli": list(self.cli),
            "console": list(self.console),
            "verify": self.fix.verify,
        }

    def payload_sha256(self) -> str:
        return hashlib.sha256(json.dumps(self.body(), sort_keys=True).encode()).hexdigest()


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


def fill(template: str, values: dict[str, str]) -> str:
    """Placeholders are `{name}` only, so Terraform's own braces pass through untouched."""

    def one(found: re.Match[str]) -> str:
        name = found.group(1)
        if name not in values:
            raise Refused(f"the template names {{{name}}}, which is not given")
        return values[name]

    return _PLACEHOLDER.sub(one, template)


def _prefix(template: str) -> list[str]:
    words: list[str] = []
    for word in template.split():
        if _PLACEHOLDER.search(word):
            break
        words.append(word)
    return words


def render_cli(fix: Fix, params: dict[str, str]) -> tuple[tuple[str, ...], Check]:
    commands: list[str] = []
    problems: list[str] = []
    quoted = {k: shlex.quote(v) for k, v in params.items()}
    for template in fix.cli:
        command = fill(template, quoted)
        if _SHELL_CONTROL.search(command):
            problems.append(f"shell control character in: {command}")
        words = shlex.split(command)
        prefix = _prefix(template)
        if words[: len(prefix)] != prefix:
            problems.append(f"does not start with {' '.join(prefix)}: {command}")
        commands.append(command)
    return tuple(commands), Check("cli", not problems, "; ".join(problems))


# -- the Terraform change ------------------------------------------------------------------


def _lines(text: str) -> list[str]:
    return text.split("\n")


def _referring(
    text: str, target: terraform.Block, key: str, types: tuple[str, ...]
) -> list[terraform.Block]:
    """Blocks of the given types that refer to the target, by address or by its name."""
    return [
        b
        for b in terraform.blocks(text)
        if b.type in types and (f"{target.address}." in b.text or f'"{key}"' in b.text)
    ]


def _apply(
    spec: TerraformSpec, text: str, target: terraform.Block, values: dict[str, str]
) -> str | None:
    lines = _lines(text)
    if spec.kind == "add_block":
        block = fill(
            spec.template or "", {**values, "address": target.address, "name": target.name}
        )
        at = target.end_line  # the line after the target's closing brace
        return "\n".join(lines[:at] + ["", *block.split("\n")] + lines[at:])
    if spec.kind == "set_attribute":
        attribute = spec.attribute or ""
        pattern = re.compile(rf"^(\s*){re.escape(attribute)}\s*=")
        inner = range(target.start_line, target.end_line - 1)  # 0-based lines inside the braces
        for i in inner:
            found = pattern.match(lines[i])
            if found:
                lines[i] = f"{found.group(1)}{attribute} = {spec.value}"
                return "\n".join(lines)
        indent = next(
            (re.match(r"^(\s*)", lines[i]).group(1) for i in inner if lines[i].strip()),  # type: ignore[union-attr]
            "  ",
        )
        lines.insert(target.end_line - 1, f"{indent}{attribute} = {spec.value}")
        return "\n".join(lines)
    removed = _referring(text, target, values.get("key", ""), spec.types)
    if not removed:
        return None
    drop: set[int] = set()
    for gone in removed:
        drop.update(range(gone.start_line - 1, gone.end_line))
        before = gone.start_line - 2
        if before >= 0 and not lines[before].strip() and before not in drop:
            drop.add(before)  # the blank line that separated it
    return "\n".join(line for i, line in enumerate(lines) if i not in drop)


def _outside(text: str) -> list[str]:
    """The file's lines outside every resource block, blank lines dropped."""
    inside: set[int] = set()
    for block in terraform.blocks(text):
        inside.update(range(block.start_line - 1, block.end_line))
    return [line for i, line in enumerate(_lines(text)) if i not in inside and line.strip()]


def scope_check(spec: TerraformSpec, before: str, after: str, target: terraform.Block) -> Check:
    old = {b.address: b for b in terraform.blocks(before)}
    new = {b.address: b for b in terraform.blocks(after)}
    added = [new[a] for a in new.keys() - old.keys()]
    removed = [old[a] for a in old.keys() - new.keys()]
    changed = [a for a in old.keys() & new.keys() if old[a].text != new[a].text]
    problems: list[str] = []
    if spec.kind == "add_block":
        allowed = set(_TEMPLATE_TYPES.findall(spec.template or ""))
        if len(added) != 1 or added[0].type not in allowed:
            problems.append(
                f"added {[b.address for b in added]}, expected one of {sorted(allowed)}"
            )
        if removed or changed:
            problems.append(f"removed {[b.address for b in removed]}, changed {changed}")
        elif added and f"{target.address}." not in added[0].text:
            problems.append(f"the new block does not refer to {target.address}")
    elif spec.kind == "set_attribute":
        if added or removed or changed != [target.address]:
            problems.append(f"changed {changed}, added {len(added)}, removed {len(removed)}")
    else:
        if added or changed or not removed or any(b.type not in spec.types for b in removed):
            problems.append(
                f"removed {[b.address for b in removed]}, added {len(added)}, changed {changed}"
            )
    if _outside(before) != _outside(after):
        problems.append("text outside the resource blocks changed")
    return Check("scope", not problems, "; ".join(problems))


def render_terraform(
    fix: Fix, params: dict[str, str], files: list[dict[str, Any]]
) -> tuple[dict[str, Any] | None, list[Check]]:
    """The change to the one file whose block of the fix's type names the resource; none
    when no file, or more than one block, names it (a person decides then)."""
    spec = fix.terraform
    if spec is None:
        return None, []
    key = params[fix.key_param]
    candidates = [
        (f, block)
        for f in files
        for block in terraform.mentioning(str(f.get("text") or ""), key, (spec.on,))
    ]
    if len(candidates) != 1:
        return None, []
    found, target = candidates[0]
    before = str(found["text"])
    after = _apply(spec, before, target, {**params, "key": key})
    if after is None:
        return None, []
    path = str(found["path"])
    diff = "".join(
        difflib.unified_diff(
            before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            fromfile=f"a/{path}",
            tofile=f"b/{path}",
        )
    )
    checks = [
        Check("syntax", terraform.balanced(after), "" if terraform.balanced(after) else path),
        scope_check(spec, before, after, target),
    ]
    change = {
        "repo": found.get("repo"),
        "path": path,
        "sha": found.get("sha"),
        "address": target.address,
        "kind": spec.kind,
        "diff": diff,
    }
    return change, checks


def render(fix: Fix, params: dict[str, Any], files: list[dict[str, Any]] | None = None) -> Rendered:
    """Check the parameters, fill the templates, and check what came out (offline)."""
    values = check_params(fix, params)
    cli, cli_check = render_cli(fix, values)
    console = tuple(fill(line, values) for line in fix.console)
    change, tf_checks = render_terraform(fix, values, files or [])
    shown = "\n".join([*cli, *console, (change or {}).get("diff", "")])
    left = sorted(set(_PLACEHOLDER.findall(shown)) & (set(values) | {"address", "name"}))
    checks = (
        Check("params", True),
        cli_check,
        *tf_checks,
        Check("placeholders", not left, ", ".join(left)),
    )
    return Rendered(fix, values, change, cli, console, checks)
