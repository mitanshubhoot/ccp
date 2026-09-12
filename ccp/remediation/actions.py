"""The action catalogue and the rule fallback (spec.md Layer 10 "Actions").

| action                    | trigger                                   | reversible | live |
|---------------------------|-------------------------------------------|------------|------|
| remove_repo_collaborator  | GH-1 or HR-1 fail, surviving repo grant   | re-invite  | yes  |
| remove_org_member         | same, org level                           | re-invite  | no   |
| notify_owner              | any fail or needs_review                  | n/a        | log  |
| request_evidence          | missing evidence (Investigator)           | n/a        | log  |

`reversible` and `live_capable` come from this table, never from a model. The rule fallback
proposes the obvious removal for a plain HR-1 or GH-1 fail so the deterministic path never
depends on a model to propose it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from ccp.evaluators.verdict import Verdict
from ccp.normalize import repository
from ccp.normalize.entities import AccessGrant
from ccp.storage.scoped import ScopedDB
from ccp.types import GrantState, VerdictStatus

ActionName = Literal[
    "remove_repo_collaborator", "remove_org_member", "notify_owner", "request_evidence"
]


@dataclass(frozen=True)
class ActionSpec:
    name: ActionName
    reversible: bool
    live_capable: bool
    description: str


CATALOGUE: dict[str, ActionSpec] = {
    "remove_repo_collaborator": ActionSpec(
        "remove_repo_collaborator",
        True,
        True,
        "Remove a direct collaborator from one repository; re-invite restores the permission.",
    ),
    "remove_org_member": ActionSpec(
        "remove_org_member",
        True,
        False,
        "Remove an organization member (dry-run only: it also drops every team membership).",
    ),
    "notify_owner": ActionSpec(
        "notify_owner", False, False, "Log a notification for the subject's owner; never live."
    ),
    "request_evidence": ActionSpec(
        "request_evidence", False, False, "Open an evidence request for a human to fulfil."
    ),
}


@dataclass(frozen=True)
class ProposalDraft:
    action: ActionName
    target: dict[str, str]
    rationale: str


def _login_of(db: ScopedDB, person_entity_id: str) -> str | None:
    for person in repository.current_persons(db):
        if person.entity_id == person_entity_id:
            return person.handles.get("github")
    return None


def _asset_name(db: ScopedDB, asset_entity_id: str) -> str | None:
    for asset in repository.current_assets(db):
        if asset.entity_id == asset_entity_id:
            return asset.name
    return None


def rule_fallback(db: ScopedDB, verdict: Verdict) -> list[ProposalDraft]:
    """Proposals from the verdict alone: the surviving GitHub grants of an HR-1/GH-1 fail."""
    drafts: list[ProposalDraft] = []
    if verdict.status is VerdictStatus.failed and verdict.test_id in ("HR-1", "GH-1"):
        grants: dict[str, AccessGrant] = {
            g.entity_id: g
            for g in repository.current_grants(db)
            if g.entity_id in verdict.evidence_refs
        }
        for grant in sorted(grants.values(), key=lambda g: g.entity_id):
            if grant.source_connector != "github" or grant.state is not GrantState.active:
                continue
            login = _login_of(db, grant.person_entity_id)
            if login is None:
                continue
            if grant.asset_entity_id.startswith("github:asset:org:"):
                org = grant.asset_entity_id.rsplit(":", 1)[-1]
                drafts.append(
                    ProposalDraft(
                        "remove_org_member",
                        {"org": org, "login": login},
                        f"{login} is still an organization member after termination"
                        f" ({verdict.subject_key})",
                    )
                )
            else:
                repo = _asset_name(db, grant.asset_entity_id)
                if repo is None:
                    continue
                drafts.append(
                    ProposalDraft(
                        "remove_repo_collaborator",
                        {"repo": repo, "login": login},
                        f"{login} still holds {grant.role} on {repo} after termination"
                        f" ({verdict.subject_key})",
                    )
                )
    if verdict.status in (VerdictStatus.failed, VerdictStatus.needs_review):
        drafts.append(
            ProposalDraft(
                "notify_owner",
                {"subject_key": verdict.subject_key},
                f"{verdict.test_id} is {verdict.status.value}: {verdict.reasoning[:200]}",
            )
        )
    return drafts
