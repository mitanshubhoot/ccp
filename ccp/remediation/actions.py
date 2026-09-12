"""The rule fallback (spec.md Layer 10 "Actions"): proposals from the verdict alone, so the
deterministic path never depends on a model to propose the obvious removal. The catalogue
itself is reference data in ccp.knowledge.actions."""

from __future__ import annotations

from dataclasses import dataclass

from ccp.evaluators.verdict import Verdict
from ccp.knowledge.actions import ACTION_NAMES, CATALOGUE, ActionName, ActionSpec
from ccp.normalize import repository
from ccp.normalize.entities import AccessGrant
from ccp.storage.scoped import ScopedDB
from ccp.types import GrantState, VerdictStatus

__all__ = [
    "ACTION_NAMES",
    "CATALOGUE",
    "ActionName",
    "ActionSpec",
    "ProposalDraft",
    "rule_fallback",
]


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
