"""`policy.check`: who may call which tool, decided outside the model (spec.md A-113).

    principal (graph:tier3, graph:investigator, graph:policy, graph:copilot, api:<role>)
      + tool + its validated arguments ──► rules ──► permit | deny | ask
                                                       └─ logged: tenant, principal, user, run,
                                                          tool, class, argument hash, rule

Default deny: a call is permitted only by a rule. The base rules are the graphs' own tool
lists (`ccp/agent/allowlists.py`), one principal per graph type; `config/tool_policy.yaml`
adds the operator's rules on top. Precedence: an enforced deny, then an enforced ask, then an
enforced permit; nothing matched is a deny. A `log_only` rule decides nothing: when it would
have permitted a call that is denied, the decision names it (`shadow_rule_id`), so a new
permit is watched before it is switched on. Conditions (`when`) compare the call's validated
arguments with fixed values the model cannot widen. Any failure inside the check, a broken
rule file included, is a deny, and a decision that cannot be logged is a deny: the check
fails closed. A tool no rule could ever permit for a principal leaves its tool list.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field
from ulid import ULID

from ccp.agent.allowlists import ALLOWLISTS
from ccp.config import REPO_ROOT
from ccp.storage.scoped import ScopedDB

Effect = Literal["permit", "deny", "ask"]
RULES_PATH = REPO_ROOT / "config" / "tool_policy.yaml"
DENIALS_STOP = 3  # this many denied calls in a row stop a run for review


class Rule(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(min_length=1)
    principal: str = Field(min_length=1)  # exact, or a prefix ending in "*" ("api:*")
    tools: tuple[str, ...] = Field(min_length=1)  # tool names, or ("*",)
    effect: Effect = "permit"
    mode: Literal["enforce", "log_only"] = "enforce"
    when: dict[str, tuple[str, ...]] = Field(default_factory=dict)

    def covers(self, principal: str, tool: str) -> bool:
        """The rule speaks to this principal and tool, whatever the arguments."""
        if self.principal.endswith("*"):
            if not principal.startswith(self.principal[:-1]):
                return False
        elif principal != self.principal:
            return False
        return "*" in self.tools or tool in self.tools

    def matches(self, principal: str, tool: str, args: Mapping[str, Any]) -> bool:
        return self.covers(principal, tool) and all(
            str(args.get(key)) in allowed for key, allowed in self.when.items()
        )


class RuleFile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rules: tuple[Rule, ...] = ()


def base_rules() -> tuple[Rule, ...]:
    """One permit per graph type, from its own tool list. The Policy Agent may search only
    policy documents, which the rule states as well as the graph's pinned argument."""
    rules = [
        Rule(id=f"base:{graph}", principal=f"graph:{graph}", tools=tuple(sorted(tools)))
        for graph, tools in sorted(ALLOWLISTS.items())
        if graph != "policy"
    ]
    rules.append(
        Rule(
            id="base:policy",
            principal="graph:policy",
            tools=tuple(sorted(t for t in ALLOWLISTS["policy"] if t != "search_evidence")),
        )
    )
    rules.append(
        Rule(
            id="base:policy-search",
            principal="graph:policy",
            tools=("search_evidence",),
            when={"evidence_type": ("policy_document",)},
        )
    )
    return tuple(rules)


@lru_cache(maxsize=8)
def _file_rules(path: Path, stamp: float) -> tuple[Rule, ...]:
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return RuleFile.model_validate(raw).rules


def rules(path: Path | None = None) -> tuple[Rule, ...]:
    """The base rules and the operator's file. Raises on a broken file (callers deny)."""
    where = path or RULES_PATH
    extra = _file_rules(where, where.stat().st_mtime) if where.exists() else ()
    return base_rules() + extra


@dataclass(frozen=True)
class Decision:
    effect: Effect
    rule_id: str | None
    reason: str
    shadow_rule_id: str | None = None

    @property
    def permitted(self) -> bool:
        return self.effect == "permit"


def check(
    principal: str, tool: str, args: Mapping[str, Any], table: Sequence[Rule] | None = None
) -> Decision:
    """The decision for one call. Never raises: any failure is a deny."""
    try:
        active = rules() if table is None else tuple(table)
        hits = [r for r in active if r.matches(principal, tool, args)]
        shadow = next((r.id for r in hits if r.mode == "log_only" and r.effect == "permit"), None)
        enforced = [r for r in hits if r.mode == "enforce"]
        for effect in ("deny", "ask", "permit"):
            rule = next((r for r in enforced if r.effect == effect), None)
            if rule is not None:
                reason = f"{effect} by {rule.id}"
                if effect == "permit":
                    return Decision("permit", rule.id, reason)
                return Decision(rule.effect, rule.id, reason, shadow)
        return Decision("deny", None, f"no rule permits {principal} to call {tool}", shadow)
    except Exception as exc:  # the check fails closed
        return Decision("deny", None, f"policy error: {type(exc).__name__}: {exc}")


def offered(principal: str, tools: Sequence[str], table: Sequence[Rule] | None = None) -> list[str]:
    """The tools some enforced permit could allow this principal, unless an unconditional
    enforced deny takes them: the rest leave its tool list. A broken rule file offers none."""
    try:
        active = rules() if table is None else tuple(table)
    except Exception:  # fail closed
        return []
    enforced = [r for r in active if r.mode == "enforce"]
    return [
        tool
        for tool in tools
        if any(r.effect == "permit" and r.covers(principal, tool) for r in enforced)
        and not any(
            r.effect == "deny" and not r.when and r.covers(principal, tool) for r in enforced
        )
    ]


def arguments_sha256(args: Mapping[str, Any]) -> str:
    return hashlib.sha256(json.dumps(args, sort_keys=True, default=str).encode("utf-8")).hexdigest()


@dataclass
class Gate:
    """One run's (or one API caller's) policy check, and its log. Every call through the
    tool gateway gets a decision here, refused calls included."""

    principal: str
    run_id: str | None = None
    user_id: str | None = None
    log_db: ScopedDB | None = None  # the decision log; tools never see it
    table: tuple[Rule, ...] | None = None  # tests pin a table; None reads the live rules
    decisions: list[tuple[str, Decision]] = field(default_factory=list)

    def offered(self, tools: Sequence[str]) -> list[str]:
        return offered(self.principal, tools, self.table)

    def decide(self, tool: str, tool_class: str, args: Mapping[str, Any]) -> Decision:
        return self.record(tool, tool_class, args, check(self.principal, tool, args, self.table))

    def record(
        self, tool: str, tool_class: str, args: Mapping[str, Any], decision: Decision
    ) -> Decision:
        """Log the decision; one that cannot be logged is a deny."""
        if self.log_db is not None:
            try:
                with self.log_db.transaction() as cur:
                    cur.execute(
                        "INSERT INTO policy_decisions (decision_id, tenant_id, principal,"
                        " user_id, run_id, tool, tool_class, arguments_sha256, effect, rule_id,"
                        " shadow_rule_id, reason) VALUES"
                        " (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                        (
                            str(ULID()),
                            self.log_db.tenant.tenant_id,
                            self.principal,
                            self.user_id,
                            self.run_id,
                            tool,
                            tool_class,
                            arguments_sha256(args),
                            decision.effect,
                            decision.rule_id,
                            decision.shadow_rule_id,
                            decision.reason[:2000],
                        ),
                    )
            except Exception as exc:  # an unlogged call is not made
                decision = Decision("deny", None, f"the decision could not be logged: {exc}")
        self.decisions.append((tool, decision))
        return decision
