"""The remediation action catalogue (spec.md Layer 10 "Actions", A-33): reference data, so it
lives with the knowledge graph and every layer above can read it.

| action                    | trigger                                   | reversible | live |
|---------------------------|-------------------------------------------|------------|------|
| remove_repo_collaborator  | GH-1 or HR-1 fail, surviving repo grant   | re-invite  | yes  |
| remove_org_member         | same, org level                           | re-invite  | no   |
| notify_owner              | any fail or needs_review                  | n/a        | log  |
| request_evidence          | missing evidence (Investigator)           | n/a        | log  |

`reversible` and `live_capable` come from this table, never from a model.
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
