"""The remediation action catalogue (spec.md Layer 10 "Actions", A-33): reference data, so it
lives with the knowledge graph and every layer above can read it.

| action                    | trigger                                   | reversible | live |
|---------------------------|-------------------------------------------|------------|------|
| remove_repo_collaborator  | GH-1 or HR-1 fail, surviving repo grant   | re-invite  | yes  |
| remove_org_member         | same, org level                           | re-invite  | no   |
| notify_owner              | any fail or needs_review                  | n/a        | log  |
| request_evidence          | missing evidence (Investigator)           | n/a        | log  |

`reversible` and `live_capable` come from this table, never from a model. So does the risk
summary a reviewer reads (RISKS), and the action that undoes each reversible one (REVERSALS),
which only a rollback creates (A-131).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

ActionName = Literal[
    "remove_repo_collaborator", "remove_org_member", "notify_owner", "request_evidence"
]
ACTION_NAMES: tuple[ActionName, ...] = (
    "remove_repo_collaborator",
    "remove_org_member",
    "notify_owner",
    "request_evidence",
)


@dataclass(frozen=True)
class ActionSpec:
    name: ActionName
    reversible: bool
    live_capable: bool
    description: str
    required_target_keys: tuple[str, ...]


CATALOGUE: dict[str, ActionSpec] = {
    "remove_repo_collaborator": ActionSpec(
        "remove_repo_collaborator",
        True,
        True,
        "Remove a direct collaborator from one repository; re-invite restores the permission.",
        ("repo", "login"),
    ),
    "remove_org_member": ActionSpec(
        "remove_org_member",
        True,
        False,
        "Remove an organization member (dry-run only: it also drops every team membership).",
        ("org", "login"),
    ),
    "notify_owner": ActionSpec(
        "notify_owner",
        False,
        False,
        "Log a notification for the subject's owner; never live.",
        (),
    ),
    "request_evidence": ActionSpec(
        "request_evidence",
        False,
        False,
        "Open an evidence request for a human to fulfil.",
        (),
    ),
}


def is_known(action: str) -> bool:
    return action in CATALOGUE


@dataclass(frozen=True)
class Risk:
    """What an action changes, in words code writes from the target (A-131). A reviewer
    reads this, never a model's account of the change."""

    changes: str  # a template over the target's keys
    reach: str
    external: bool  # changes a system outside CCP
    undo: str  # a template over the target's keys


RISKS: dict[str, Risk] = {
    "remove_repo_collaborator": Risk(
        "{login} loses direct access to {repo}",
        "one collaborator on one repository",
        True,
        "a rollback re-invites {login} to {repo} with the permission read just before the"
        " removal; it is a proposal of its own and needs its own approval",
    ),
    "remove_org_member": Risk(
        "{login} leaves the {org} organization and every team in it",
        "one member of one organization, with all of their team memberships",
        True,
        "a rollback re-invites {login} to {org}; team memberships are not restored",
    ),
    "notify_owner": Risk(
        "a notification for the owner of {subject_key} is logged in CCP",
        "a log line inside CCP",
        False,
        "nothing to undo",
    ),
    "request_evidence": Risk(
        "an evidence request opens for a person to fulfil",
        "one request inside CCP",
        False,
        "close the request",
    ),
    "restore_repo_collaborator": Risk(
        "{login} is re-invited to {repo} with {permission}",
        "one collaborator on one repository",
        True,
        "removing {login} again is a new proposal",
    ),
    "restore_org_member": Risk(
        "{login} is re-invited to the {org} organization",
        "one member of one organization",
        True,
        "removing {login} again is a new proposal",
    ),
}

# The action that undoes each reversible one. Only a rollback creates these, from the
# pre-state CCP recorded; they are not in CATALOGUE, so no model can propose one.
REVERSALS: dict[str, str] = {
    "remove_repo_collaborator": "restore_repo_collaborator",
    "remove_org_member": "restore_org_member",
}


class _Blank(dict[str, str]):
    def __missing__(self, key: str) -> str:
        return f"<{key}?>"


def risk_summary(
    action: str, target: dict[str, str], *, reversible: bool, live_capable: bool
) -> dict[str, object]:
    """The risk summary shown with an approval and covered by its payload hash."""
    risk = RISKS[action]
    keys = _Blank(target)
    return {
        "changes": risk.changes.format_map(keys),
        "reach": risk.reach,
        "external": risk.external,
        "reversible": reversible,
        "undo": risk.undo.format_map(keys),
        "runs": "live behind the five gates, or as a dry run" if live_capable else "dry run only",
    }
