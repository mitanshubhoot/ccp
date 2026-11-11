"""The five live-mode gates (spec.md Layer 10 "Live-mode gate"). All must hold at once.

1  CCP_REMEDIATION_LIVE=1
2  --live passed on the command line
3  the target repo is in allowed_repos and the login is not in protected_logins
4  the operator typed exactly `remove <login> from <org>/<repo>`
   (a rollback: `restore <login> to <org>/<repo>`)
5  the proposal's approval item is approved by a named reviewer and its payload hash
   still matches the proposal (an approval cannot be reused for an edited proposal); a
   rollback is a proposal of its own with its own approved item, bound the same way
   (A-95, A-131)

Past the gates, the executor still needs the decision's grant, unused and unexpired, and
the precondition the proposal was made under (runner.py, A-131).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field

from ccp.approvals.store import Approval
from ccp.config import Settings
from ccp.remediation.proposals import Proposal


class RemediationConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    allowed_repos: list[str] = Field(default_factory=list)  # "org/repo"
    protected_logins: list[str] = Field(default_factory=list)


def load_config(path: Path) -> RemediationConfig:
    if not path.is_file():
        return RemediationConfig()
    return RemediationConfig.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")) or {})


def confirmation_phrase(org: str, repo: str, login: str) -> str:
    return f"remove {login} from {org}/{repo}"


def rollback_phrase(org: str, repo: str, login: str) -> str:
    return f"restore {login} to {org}/{repo}"


def approval_matches(approval: Approval | None, expected_sha256: str) -> bool:
    """Gate 5: approved, by a named reviewer, for exactly this payload."""
    return (
        approval is not None
        and approval.state == "approved"
        and bool(approval.reviewer.strip())
        and approval.payload_sha256 == expected_sha256
    )


@dataclass(frozen=True)
class GateCheck:
    missing: tuple[str, ...]

    @property
    def passed(self) -> bool:
        return not self.missing

    def describe(self) -> str:
        return "all five gates hold" if self.passed else "missing gates: " + ", ".join(self.missing)


def check(
    settings: Settings,
    proposal: Proposal,
    *,
    org: str,
    live_flag: bool,
    confirmation: str | None,
    approval: Approval | None,
    config: RemediationConfig | None = None,
) -> GateCheck:
    cfg = config or load_config(settings.ccp_remediation_config)
    owner, _, name = proposal.target.get("repo", "").partition("/")
    repo = name or owner  # targets carry "org/repo"; tolerate a bare repo name
    full_name = f"{owner if name else org}/{repo}"
    login = proposal.target.get("login", "")
    phrase = (rollback_phrase if proposal.reverses else confirmation_phrase)(org, repo, login)
    expected = proposal.current_hash()
    missing: list[str] = []
    if not settings.ccp_remediation_live:
        missing.append("gate1:CCP_REMEDIATION_LIVE")
    if not live_flag:
        missing.append("gate2:--live")
    if full_name not in cfg.allowed_repos or login in cfg.protected_logins or not login:
        missing.append("gate3:allowlist")
    if confirmation is None or confirmation.strip() != phrase:
        missing.append("gate4:confirmation")
    if not approval_matches(approval, expected):
        missing.append("gate5:approval")
    return GateCheck(tuple(missing))
