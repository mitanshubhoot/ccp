"""Validated, bounded targets (spec.md R12, S19): every value of a proposal's target must
resolve to an entity of this tenant AND the grant it would change must appear in the
originating verdict's evidence_refs. A proposal that names another tenant's repo, or a
login the verdict never cited, is dropped with a guardrail event. Shared by the rule
fallback, the Investigator runner, and the Copilot runner."""

from __future__ import annotations

from dataclasses import dataclass, field

from ccp.evaluators.verdict import Verdict
from ccp.normalize import repository
from ccp.storage.scoped import ScopedDB
from ccp.types import GrantState


@dataclass(frozen=True)
class Resolution:
    ok: bool
    reason: str = ""
    grant_entity_ids: tuple[str, ...] = ()
    events: tuple[str, ...] = field(default_factory=tuple)


def resolve(db: ScopedDB, verdict: Verdict, action: str, target: dict[str, str]) -> Resolution:
    if action in ("notify_owner", "request_evidence"):
        return Resolution(True)
    login = target.get("login", "")
    persons = {
        p.entity_id: p for p in repository.current_persons(db) if p.handles.get("github") == login
    }
    if not persons:
        return Resolution(
            False,
            f"login {login!r} is not a GitHub account of this tenant",
            events=(f"target_rejected:unknown_login:{login}",),
        )
    grants = [
        g
        for g in repository.current_grants(db)
        if g.person_entity_id in persons and g.source_connector == "github"
    ]
    if action == "remove_repo_collaborator":
        repo = target.get("repo", "")
        assets = {
            a.entity_id
            for a in repository.current_assets(db)
            if a.asset_type == "repo" and a.name == repo
        }
        if not assets:
            return Resolution(
                False,
                f"repo {repo!r} is not a repository of this tenant",
                events=(f"target_rejected:unknown_repo:{repo}",),
            )
        matching = [
            g for g in grants if g.asset_entity_id in assets and g.state is GrantState.active
        ]
    elif action == "remove_org_member":
        org = target.get("org", "")
        if org != db.tenant.settings.github_org:
            return Resolution(
                False,
                f"org {org!r} is not this tenant's organization",
                events=(f"target_rejected:unknown_org:{org}",),
            )
        matching = [
            g
            for g in grants
            if g.asset_entity_id == f"github:asset:org:{org}" and g.state is GrantState.active
        ]
    else:
        return Resolution(
            False,
            f"unknown action {action!r}",
            events=(f"target_rejected:unknown_action:{action}",),
        )
    cited = [g.entity_id for g in matching if g.entity_id in verdict.evidence_refs]
    if not cited:
        return Resolution(
            False,
            "the grant to change is not cited by the originating verdict",
            events=(f"target_rejected:uncited_grant:{action}:{login}",),
        )
    return Resolution(True, grant_entity_ids=tuple(cited))
