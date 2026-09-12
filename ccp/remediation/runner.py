"""Propose, dry-run, execute, roll back (spec.md Layer 10).

propose(db, verdict)          rule fallback -> validated targets -> proposals + approval items
execute(db, settings, id,     dry run by default: computes the exact call, records it, logs a
        live=False)           step with mode=dry_run; live only when all five gates hold, with
                              the pre-state captured first
simulate(db, settings, id)    fixture mode: gate 5 only, marks the proposal simulated (nothing
                              real exists to change); the test-only fixture hook applies it
rollback(db, settings, id)    re-invite with the recorded prior permission
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from ccp.approvals import store as approvals
from ccp.config import Settings
from ccp.evaluators import verdict as verdicts
from ccp.evaluators.verdict import Verdict
from ccp.observability import steps
from ccp.remediation import actions, gates, github_admin, targets
from ccp.remediation import proposals as proposal_store
from ccp.remediation.proposals import Proposal
from ccp.storage.scoped import ScopedDB


@dataclass
class ProposeSummary:
    proposals: list[Proposal] = field(default_factory=list)
    rejected: list[str] = field(default_factory=list)  # guardrail events


def propose(db: ScopedDB, verdict: Verdict, source: str = "rule") -> ProposeSummary:
    """Rule-fallback proposals for a verdict, each validated against the tenant and the
    verdict's evidence, each waiting in the approval stage. Idempotent per verdict."""
    summary = ProposeSummary()
    if proposal_store.for_verdict(db, verdict.verdict_id):
        summary.proposals = proposal_store.for_verdict(db, verdict.verdict_id)
        return summary
    with steps.step(
        "remediate.propose",
        layer="remediation",
        tenant=db.tenant,
        inputs={"verdict_id": verdict.verdict_id, "source": source},
    ) as s:
        for draft in actions.rule_fallback(db, verdict):
            resolution = targets.resolve(db, verdict, draft.action, draft.target)
            if not resolution.ok:
                summary.rejected.extend(resolution.events)
                for event in resolution.events:
                    s.event(event)
                continue
            spec = actions.CATALOGUE[draft.action]
            summary.proposals.append(
                proposal_store.create(
                    db,
                    verdict_id=verdict.verdict_id,
                    source=source,
                    action=draft.action,
                    target=draft.target,
                    rationale=draft.rationale,
                    reversible=spec.reversible,
                    live_capable=spec.live_capable,
                )
            )
        s.set_output(
            {"proposals": [p.proposal_id for p in summary.proposals], "rejected": summary.rejected}
        )
    return summary


def propose_for_verdict_id(db: ScopedDB, verdict_id: str) -> ProposeSummary:
    verdict = verdicts.load(db, verdict_id)
    if verdict is None:
        raise LookupError(f"no verdict {verdict_id!r}")
    return propose(db, verdict)


def planned_call(proposal: Proposal, org: str) -> github_admin.PlannedCall | None:
    if proposal.action == "remove_repo_collaborator":
        owner, _, repo = proposal.target["repo"].partition("/")
        return github_admin.plan_remove_collaborator(
            owner or org, repo or proposal.target["repo"], proposal.target["login"]
        )
    if proposal.action == "remove_org_member":
        return github_admin.plan_remove_org_member(
            proposal.target.get("org", org), proposal.target["login"]
        )
    return None


@dataclass(frozen=True)
class ExecutionResult:
    proposal: Proposal
    mode: str  # dry_run | refused | executed | simulated | rolled_back
    detail: str


def execute(
    db: ScopedDB,
    settings: Settings,
    proposal_id: str,
    *,
    live: bool = False,
    confirm: Callable[[str], str] | None = None,
    github: github_admin.GitHubAdmin | None = None,
    config: gates.RemediationConfig | None = None,
) -> ExecutionResult:
    proposal = proposal_store.get(db, proposal_id)
    if proposal is None:
        raise LookupError(f"no proposal {proposal_id!r}")
    if proposal.status in ("executed", "rolled_back", "rejected"):
        return ExecutionResult(proposal, "refused", f"proposal is {proposal.status}")
    org = db.tenant.settings.github_org
    call = planned_call(proposal, org)
    approval = approvals.for_ref(db, "remediation", proposal_id)
    with steps.step(
        "remediate.execute",
        layer="remediation",
        tenant=db.tenant,
        inputs={
            "proposal_id": proposal_id,
            "action": proposal.action,
            "target": proposal.target,
            "mode": "live" if live else "dry_run",
        },
    ) as s:
        if call is None:  # notify_owner, request_evidence: log lines, never live
            updated = proposal_store.update(
                db, proposal_id, status="dry_run", planned_call={"log": proposal.rationale}
            )
            s.set_output({"mode": "dry_run", "call": None})
            return ExecutionResult(updated, "dry_run", f"logged: {proposal.rationale}")
        if not live:
            updated = proposal_store.update(
                db, proposal_id, status="dry_run", planned_call=call.as_dict()
            )
            s.set_output({"mode": "dry_run", "call": call.as_dict()})
            return ExecutionResult(updated, "dry_run", f"would call {call.method} {call.path}")
        if not proposal.live_capable:
            updated = proposal_store.update(
                db,
                proposal_id,
                status="refused",
                planned_call=call.as_dict(),
                refusal=f"{proposal.action} is not live-capable in this build",
            )
            s.event("refused:not_live_capable")
            return ExecutionResult(updated, "refused", updated.refusal or "")
        repo = proposal.target.get("repo", "")
        owner, _, repo_name = repo.partition("/")
        login = proposal.target.get("login", "")
        confirmation = (
            confirm(
                f"type `{gates.confirmation_phrase(org, repo_name or repo, login)}` to proceed: "
            )
            if confirm
            else None
        )
        check = gates.check(
            settings,
            proposal,
            org=org,
            live_flag=live,
            confirmation=confirmation,
            approval=approval,
            config=config,
        )
        if not check.passed:
            updated = proposal_store.update(
                db,
                proposal_id,
                status="refused",
                planned_call=call.as_dict(),
                refusal=check.describe(),
            )
            for gate in check.missing:
                s.event(f"refused:{gate}")
            s.set_output({"mode": "refused", "missing": list(check.missing)})
            return ExecutionResult(updated, "refused", check.describe())
        if github is None:
            if settings.github_token is None:
                updated = proposal_store.update(
                    db, proposal_id, status="refused", refusal="GITHUB_TOKEN is not set"
                )
                return ExecutionResult(updated, "refused", "GITHUB_TOKEN is not set")
            github = github_admin.LiveGitHubAdmin(settings.github_token.get_secret_value())
        # pre-state first, so the action is reversible
        permission = github.collaborator_permission(owner or org, repo_name or repo, login)
        proposal_store.update(db, proposal_id, pre_state={"permission": permission})
        result = github.remove_collaborator(owner or org, repo_name or repo, login)
        updated = proposal_store.update(
            db,
            proposal_id,
            status="executed",
            planned_call=call.as_dict(),
            executed_call={**call.as_dict(), "result": result},
        )
        s.set_output({"mode": "executed", "call": call.as_dict(), "pre_state": permission})
        return ExecutionResult(updated, "executed", f"executed {call.method} {call.path}")


def simulate(db: ScopedDB, settings: Settings, proposal_id: str) -> ExecutionResult:
    """Fixture mode: nothing real exists to change, so only gate 5 applies. The caller then
    re-pulls through the test-only fixture overlay and re-evaluates."""
    proposal = proposal_store.get(db, proposal_id)
    if proposal is None:
        raise LookupError(f"no proposal {proposal_id!r}")
    approval = approvals.for_ref(db, "remediation", proposal_id)
    org = db.tenant.settings.github_org
    call = planned_call(proposal, org)
    with steps.step(
        "remediate.simulate",
        layer="remediation",
        tenant=db.tenant,
        inputs={"proposal_id": proposal_id, "action": proposal.action},
    ) as s:
        if proposal.status in ("executed", "rolled_back", "rejected", "simulated"):
            return ExecutionResult(proposal, "refused", f"proposal is {proposal.status}")
        if (
            approval is None
            or approval.state != "approved"
            or approval.payload_sha256 != proposal.current_hash()
        ):
            updated = proposal_store.update(
                db, proposal_id, status="refused", refusal="gate5:approval"
            )
            s.event("refused:gate5:approval")
            return ExecutionResult(updated, "refused", "missing gates: gate5:approval")
        updated = proposal_store.update(
            db,
            proposal_id,
            status="simulated",
            planned_call=call.as_dict() if call else {"log": proposal.rationale},
            pre_state={"simulated": True},
        )
        s.set_output({"mode": "simulated"})
        return ExecutionResult(updated, "simulated", "applied to the fixture overlay")


def rollback(
    db: ScopedDB,
    settings: Settings,
    proposal_id: str,
    github: github_admin.GitHubAdmin | None = None,
) -> ExecutionResult:
    proposal = proposal_store.get(db, proposal_id)
    if proposal is None:
        raise LookupError(f"no proposal {proposal_id!r}")
    if proposal.status not in ("executed", "simulated"):
        return ExecutionResult(
            proposal, "refused", f"nothing to roll back: proposal is {proposal.status}"
        )
    if not proposal.reversible:
        return ExecutionResult(proposal, "refused", f"{proposal.action} is not reversible")
    org = db.tenant.settings.github_org
    with steps.step(
        "remediate.rollback",
        layer="remediation",
        tenant=db.tenant,
        inputs={"proposal_id": proposal_id},
    ) as s:
        if proposal.status == "simulated":
            updated = proposal_store.update(db, proposal_id, status="rolled_back")
            s.set_output({"mode": "rolled_back", "simulated": True})
            return ExecutionResult(updated, "rolled_back", "simulated proposal rolled back")
        permission = str((proposal.pre_state or {}).get("permission") or "write")
        repo = proposal.target.get("repo", "")
        owner, _, repo_name = repo.partition("/")
        if github is None:
            if settings.github_token is None:
                return ExecutionResult(proposal, "refused", "GITHUB_TOKEN is not set")
            github = github_admin.LiveGitHubAdmin(settings.github_token.get_secret_value())
        call = github_admin.plan_add_collaborator(
            owner or org, repo_name or repo, proposal.target["login"], permission
        )
        result = github.add_collaborator(
            owner or org, repo_name or repo, proposal.target["login"], permission
        )
        updated = proposal_store.update(
            db,
            proposal_id,
            status="rolled_back",
            executed_call={**call.as_dict(), "result": result},
        )
        s.set_output({"mode": "rolled_back", "call": call.as_dict()})
        return ExecutionResult(updated, "rolled_back", f"re-invited with {permission}")


def describe(proposal: Proposal, org: str) -> dict[str, Any]:
    call = planned_call(proposal, org)
    return {
        "proposal_id": proposal.proposal_id,
        "verdict_id": proposal.verdict_id,
        "action": proposal.action,
        "target": proposal.target,
        "status": proposal.status,
        "reversible": proposal.reversible,
        "live_capable": proposal.live_capable,
        "planned_call": call.as_dict() if call else None,
        "pre_state": proposal.pre_state,
        "refusal": proposal.refusal,
        "rationale": proposal.rationale,
    }
