"""Propose, dry-run, execute, roll back (spec.md Layer 10, A-131).

propose(db, verdict)          rule fallback -> validated targets -> proposals + approval items
execute(db, settings, id,     dry run by default: computes the exact call and records it. Live
        live=False)           only when all five gates hold, the decision's grant is unused and
                              unexpired, and the precondition the proposal was made under still
                              holds; the grant is spent and the journal opened before anything
                              is read or changed, and the pre-state is journalled before the call
simulate(db, settings, id)    fixture mode: gate 5, the grant and the precondition (nothing real
                              exists to change); the test-only fixture hook applies it
rollback(db, settings, id,    a rollback is a proposal of its own (source rollback) that
         live=False)          restores the recorded pre-state: the first call creates it with
                              its approval item, and once approved it runs through the same
                              executor, simulated when the original was
audit(db)                     actions without an approval, a spent grant, or a journal record

A failed or interrupted attempt spends its grant: trying again opens a new approval item,
because a decision authorizes one attempt.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from ccp.approvals import grants
from ccp.approvals import store as approvals
from ccp.config import Settings
from ccp.evaluators import verdict as verdicts
from ccp.evaluators.verdict import Verdict
from ccp.knowledge.actions import REVERSALS
from ccp.observability import steps
from ccp.remediation import actions, broker, gates, github_admin, journal, preconditions, targets
from ccp.remediation import proposals as proposal_store
from ccp.remediation.proposals import ROLLBACK_KIND, Proposal
from ccp.storage.scoped import ScopedDB

__all__ = ["ROLLBACK_KIND", "ExecutionResult", "ProposeSummary"]

DONE = proposal_store.FINAL


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


def _repo(proposal: Proposal, org: str) -> tuple[str, str]:
    """(owner, repository) of a target that names `org/repo`, or a bare repository."""
    owner, _, name = proposal.target.get("repo", "").partition("/")
    return (owner, name) if name else (org, owner)


def planned_call(proposal: Proposal, org: str) -> github_admin.PlannedCall | None:
    login = proposal.target.get("login", "")
    if proposal.action == "remove_repo_collaborator":
        return github_admin.plan_remove_collaborator(*_repo(proposal, org), login)
    if proposal.action == "restore_repo_collaborator" and proposal.target.get("permission"):
        owner, repo = _repo(proposal, org)
        return github_admin.plan_add_collaborator(owner, repo, login, proposal.target["permission"])
    if proposal.action == "remove_org_member":
        return github_admin.plan_remove_org_member(proposal.target.get("org", org), login)
    return None


@dataclass(frozen=True)
class ExecutionResult:
    proposal: Proposal
    mode: str  # dry_run | refused | executed | simulated | rolled_back | failed
    detail: str


def _get(db: ScopedDB, proposal_id: str) -> Proposal:
    proposal = proposal_store.get(db, proposal_id)
    if proposal is None:
        raise LookupError(f"no proposal {proposal_id!r}")
    return proposal


def _reversed(db: ScopedDB, proposal: Proposal) -> Proposal | None:
    return proposal_store.get(db, proposal.reverses) if proposal.reverses else None


def _cleared(
    db: ScopedDB, proposal: Proposal, approval: approvals.Approval
) -> tuple[grants.Grant | None, str, bool]:
    """(the decision's grant, "", False) when it may be spent now; otherwise (None, why
    not, whether a new decision is the way on)."""
    grant = grants.for_approval(db, approval.approval_id)
    if grant is None:
        return None, f"approval {approval.approval_id} minted no grant", True
    if grant.payload_sha256 != proposal.current_hash():
        return None, f"grant {grant.grant_id} is for another payload", False
    used = grants.use_of(db, grant.grant_id)
    if used is not None:
        return None, f"grant {grant.grant_id} was spent at {used.used_at:%Y-%m-%d %H:%M} UTC", True
    if grant.expired():
        return (
            None,
            f"grant {grant.grant_id} expired at {grant.expires_at:%Y-%m-%d %H:%M} UTC",
            True,
        )
    now = preconditions.current(
        db,
        verdict_id=proposal.verdict_id,
        action=proposal.action,
        target=proposal.target,
        reverses=_reversed(db, proposal),
    )
    held = preconditions.digest(now)
    if held != proposal.precondition_sha256 or grant.precondition_sha256 != held:
        changed = "; ".join(preconditions.differences(proposal.precondition or {}, now))
        return (
            None,
            f"precondition changed since the proposal ({changed or 'the grant names another'});"
            " nothing was done; propose again",
            False,
        )
    return grant, "", False


def _refuse(
    db: ScopedDB,
    proposal: Proposal,
    s: steps.StepRecord,
    refusal: str,
    event: str,
    *,
    ask_again: bool = False,
    planned: dict[str, Any] | None = None,
) -> ExecutionResult:
    s.event(f"refused:{event}")
    if ask_again:
        item = approvals.open_item(db, proposal.kind, proposal.proposal_id, proposal.payload())
        refusal = f"{refusal}; a new decision is needed: approval {item}"
    if planned is not None:
        updated = proposal_store.record(
            db, proposal.proposal_id, "refused", refusal=refusal, planned_call=planned
        )
    else:
        updated = proposal_store.record(db, proposal.proposal_id, "refused", refusal=refusal)
    s.set_output({"mode": "refused", "refusal": refusal})
    return ExecutionResult(updated, "refused", refusal)


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
    proposal = _get(db, proposal_id)
    if proposal.status in DONE:
        return ExecutionResult(proposal, "refused", f"proposal is {proposal.status}")
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
        return _execute(
            db, settings, proposal, s, live=live, confirm=confirm, github=github, config=config
        )


def _execute(
    db: ScopedDB,
    settings: Settings,
    proposal: Proposal,
    s: steps.StepRecord,
    *,
    live: bool,
    confirm: Callable[[str], str] | None,
    github: github_admin.GitHubAdmin | None,
    config: gates.RemediationConfig | None,
) -> ExecutionResult:
    org = db.tenant.settings.github_org
    tenant_id = db.tenant.tenant_id
    proposal_id = proposal.proposal_id
    call = planned_call(proposal, org)
    if call is None:  # notify_owner, request_evidence: log lines, never live
        updated = proposal_store.record(
            db, proposal_id, "dry_run", planned_call={"log": proposal.rationale}
        )
        s.set_output({"mode": "dry_run", "call": None})
        return ExecutionResult(updated, "dry_run", f"logged: {proposal.rationale}")
    plan = call.as_dict()
    if not live:
        updated = proposal_store.record(db, proposal_id, "dry_run", planned_call=plan)
        s.set_output({"mode": "dry_run", "call": plan})
        return ExecutionResult(updated, "dry_run", f"would call {call.method} {call.path}")
    if not proposal.live_capable:
        return _refuse(
            db,
            proposal,
            s,
            f"{proposal.action} is not live-capable in this build",
            "not_live_capable",
            planned=plan,
        )
    owner, repo = _repo(proposal, org)
    login = proposal.target.get("login", "")
    phrase = (gates.rollback_phrase if proposal.reverses else gates.confirmation_phrase)(
        org, repo, login
    )
    confirmation = confirm(f"type `{phrase}` to proceed: ") if confirm else None
    approval = approvals.for_ref(db, proposal.kind, proposal_id)
    check = gates.check(
        settings,
        proposal,
        org=org,
        live_flag=True,
        confirmation=confirmation,
        approval=approval,
        config=config,
    )
    if not check.passed:
        for gate in check.missing[1:]:
            s.event(f"refused:{gate}")
        return _refuse(db, proposal, s, check.describe(), check.missing[0], planned=plan)
    assert approval is not None
    grant, problem, ask_again = _cleared(db, proposal, approval)
    if grant is None:
        reason = "precondition" if problem.startswith("precondition") else "grant"
        return _refuse(db, proposal, s, problem, reason, ask_again=ask_again, planned=plan)
    if github is None and not broker.available(settings):  # before the grant is spent
        return _refuse(db, proposal, s, "GITHUB_TOKEN is not set", "no_credential", planned=plan)
    operation = journal.operation_key(proposal_id, proposal.payload_sha256)
    try:
        with db.transaction() as cur:
            grants.consume(cur, tenant_id, grant, operation)
            journal.append(
                cur,
                tenant_id,
                operation=operation,
                proposal_id=proposal_id,
                grant_id=grant.grant_id,
                entry="started",
                mode="live",
                detail={"call": plan},
            )
    except grants.GrantUsed as exc:
        return _refuse(db, proposal, s, str(exc), "grant", planned=plan)
    try:
        if github is None:  # the write credential comes from the broker alone (A-116)
            verb = "add_collaborator" if proposal.reverses else "remove_collaborator"
            credential = broker.issue(
                db,
                settings,
                operation=proposal_id,
                action="rollback" if proposal.reverses else "execute",
                approval=approval,
                scope=f"github:{owner}/{repo}:{verb}:{login}",
                grant=grant,
            )
            if credential is None:
                raise RuntimeError("GITHUB_TOKEN went away after the grant was spent")
            github = github_admin.LiveGitHubAdmin(credential.token)
        permission = github.collaborator_permission(owner, repo, login)
        with db.transaction() as cur:  # the pre-state first, so the action can be undone
            journal.append(
                cur,
                tenant_id,
                operation=operation,
                proposal_id=proposal_id,
                grant_id=grant.grant_id,
                entry="observed",
                mode="live",
                detail={"permission": permission},
            )
        if proposal.reverses:
            result = github.add_collaborator(owner, repo, login, proposal.target["permission"])
        else:
            result = github.remove_collaborator(owner, repo, login)
    except Exception as exc:  # whatever stopped the call, the journal records it
        return _failed(db, proposal, grant, operation, plan, exc, s)
    executed = {**plan, "result": result}
    with db.transaction() as cur:
        journal.append(
            cur,
            tenant_id,
            operation=operation,
            proposal_id=proposal_id,
            grant_id=grant.grant_id,
            entry="finished",
            mode="live",
            detail={"result": result},
        )
        proposal_store.record_in(
            cur,
            tenant_id,
            proposal_id,
            "executed",
            pre_state={"permission": permission},
            planned_call=plan,
            executed_call=executed,
        )
        if proposal.reverses:
            proposal_store.record_in(
                cur, tenant_id, proposal.reverses, "rolled_back", executed_call=executed
            )
    s.set_output(
        {"mode": "executed", "call": plan, "pre_state": permission, "grant": grant.grant_id}
    )
    return ExecutionResult(_get(db, proposal_id), "executed", f"executed {call.method} {call.path}")


def _failed(
    db: ScopedDB,
    proposal: Proposal,
    grant: grants.Grant,
    operation: str,
    plan: dict[str, Any],
    exc: Exception,
    s: steps.StepRecord,
) -> ExecutionResult:
    """The call did not complete: the journal and the proposal say so, the grant stays
    spent, and a new approval item asks again."""
    error = f"{type(exc).__name__}: {str(exc)[:300]}"
    with db.transaction() as cur:
        journal.append(
            cur,
            db.tenant.tenant_id,
            operation=operation,
            proposal_id=proposal.proposal_id,
            grant_id=grant.grant_id,
            entry="failed",
            mode="live",
            detail={"error": error},
        )
        proposal_store.record_in(
            cur,
            db.tenant.tenant_id,
            proposal.proposal_id,
            "failed",
            planned_call=plan,
            refusal=f"the call failed: {error}",
        )
    item = approvals.open_item(db, proposal.kind, proposal.proposal_id, proposal.payload())
    s.event("failed")
    s.set_output({"mode": "failed", "error": error, "approval_id": item})
    return ExecutionResult(
        _get(db, proposal.proposal_id),
        "failed",
        f"the call failed ({error}); its grant is spent, and approval {item} asks again",
    )


def simulate(db: ScopedDB, settings: Settings, proposal_id: str) -> ExecutionResult:
    """Fixture mode: nothing real exists to change, so gates 1 to 4 do not apply; gate 5,
    the grant and the precondition do. The caller then re-pulls through the test-only
    fixture overlay and re-evaluates."""
    proposal = _get(db, proposal_id)
    with steps.step(
        "remediate.simulate",
        layer="remediation",
        tenant=db.tenant,
        inputs={"proposal_id": proposal_id, "action": proposal.action},
    ) as s:
        if proposal.status in DONE:
            return ExecutionResult(proposal, "refused", f"proposal is {proposal.status}")
        return _simulate(db, proposal, s)


def _simulate(db: ScopedDB, proposal: Proposal, s: steps.StepRecord) -> ExecutionResult:
    tenant_id = db.tenant.tenant_id
    call = planned_call(proposal, db.tenant.settings.github_org)
    plan = call.as_dict() if call else {"log": proposal.rationale}
    approval = approvals.for_ref(db, proposal.kind, proposal.proposal_id)
    if approval is None or not gates.approval_matches(approval, proposal.current_hash()):
        return _refuse(db, proposal, s, "missing gates: gate5:approval", "gate5:approval")
    grant, problem, ask_again = _cleared(db, proposal, approval)
    if grant is None:
        reason = "precondition" if problem.startswith("precondition") else "grant"
        return _refuse(db, proposal, s, problem, reason, ask_again=ask_again)
    operation = journal.operation_key(proposal.proposal_id, proposal.payload_sha256)
    try:
        with db.transaction() as cur:
            grants.consume(cur, tenant_id, grant, operation)
            for entry, detail in (("started", {"call": plan}), ("finished", {"simulated": True})):
                journal.append(
                    cur,
                    tenant_id,
                    operation=operation,
                    proposal_id=proposal.proposal_id,
                    grant_id=grant.grant_id,
                    entry=entry,  # type: ignore[arg-type]
                    mode="simulated",
                    detail=detail,
                )
            proposal_store.record_in(
                cur,
                tenant_id,
                proposal.proposal_id,
                "simulated",
                planned_call=plan,
                pre_state={"simulated": True},
            )
            if proposal.reverses:
                proposal_store.record_in(cur, tenant_id, proposal.reverses, "rolled_back")
    except grants.GrantUsed as exc:
        return _refuse(db, proposal, s, str(exc), "grant")
    s.set_output({"mode": "simulated", "grant": grant.grant_id})
    return ExecutionResult(
        _get(db, proposal.proposal_id), "simulated", "applied to the fixture overlay"
    )


def _reversal_target(original: Proposal) -> dict[str, str] | None:
    """What a rollback restores: for a live removal, the permission read before it; a
    simulated one restores the fixture state. None when nothing was recorded, since
    restoring a guessed permission could grant more than was removed."""
    if original.status == "simulated":
        return dict(original.target)
    permission = (original.pre_state or {}).get("permission")
    return {**original.target, "permission": str(permission)} if permission else None


def _reversal(db: ScopedDB, original: Proposal, target: dict[str, str]) -> Proposal:
    """The rollback proposal for this target, created if none stands; an open request for
    another target is closed, so a reviewer sees only what the rollback would now do."""
    made = proposal_store.reversals(db, original.proposal_id)
    chosen = next(
        (r for r in reversed(made) if r.target == target and r.status not in ("rejected",)),
        None,
    )
    restores = target.get("permission", "the simulated state")
    for other in made:
        if chosen is not None and other.proposal_id == chosen.proposal_id:
            continue
        for item in approvals.items_for_ref(db, ROLLBACK_KIND, other.proposal_id):
            if item.is_open:
                approvals.record_event(
                    db,
                    item.approval_id,
                    "closed",
                    "system",
                    f"superseded: the rollback would now restore {restores}",
                )
    return chosen or proposal_store.create_reversal(db, original, target)


def reversal_of(db: ScopedDB, proposal_id: str) -> Proposal | None:
    """The newest rollback proposal made for a proposal."""
    made = proposal_store.reversals(db, proposal_id)
    return made[-1] if made else None


def rollback_requests(db: ScopedDB, proposal_id: str) -> list[approvals.Approval]:
    """Every approval item of every rollback proposal made for a proposal, oldest first."""
    return [
        item
        for reversal in proposal_store.reversals(db, proposal_id)
        for item in approvals.items_for_ref(db, ROLLBACK_KIND, reversal.proposal_id)
    ]


def rollback(
    db: ScopedDB,
    settings: Settings,
    proposal_id: str,
    *,
    live: bool = False,
    confirm: Callable[[str], str] | None = None,
    github: github_admin.GitHubAdmin | None = None,
    config: gates.RemediationConfig | None = None,
) -> ExecutionResult:
    """A rollback is an action like any other (A-95), and a proposal of its own (A-131):
    the first call creates it, restoring the pre-state CCP recorded, and opens its
    remediation_rollback item. Approved, it runs through the same executor: simulated when
    the original was; otherwise a dry run without --live, and all five gates, its own grant
    and its precondition with it."""
    original = _get(db, proposal_id)
    if original.status not in ("executed", "simulated"):
        return ExecutionResult(
            original, "refused", f"nothing to roll back: proposal is {original.status}"
        )
    if not original.reversible or original.action not in REVERSALS:
        return ExecutionResult(original, "refused", f"{original.action} is not reversible")
    with steps.step(
        "remediate.rollback",
        layer="remediation",
        tenant=db.tenant,
        inputs={"proposal_id": proposal_id, "mode": "live" if live else "dry_run"},
    ) as s:
        target = _reversal_target(original)
        if target is None:
            s.event("refused:no_pre_state")
            return ExecutionResult(
                original, "refused", "no recorded prior permission; nothing to restore"
            )
        reversal = _reversal(db, original, target)
        approval = approvals.for_ref(db, ROLLBACK_KIND, reversal.proposal_id)
        if approval is None or not gates.approval_matches(approval, reversal.current_hash()):
            item = approvals.open_item(db, ROLLBACK_KIND, reversal.proposal_id, reversal.payload())
            s.event("refused:gate5:approval")
            s.set_output(
                {"mode": "refused", "proposal_id": reversal.proposal_id, "approval_id": item}
            )
            return ExecutionResult(
                original,
                "refused",
                f"missing gates: gate5:approval; rollback proposal {reversal.proposal_id} waits"
                f" for approval {item}",
            )
        if reversal.status in DONE:
            return ExecutionResult(original, "refused", f"rollback is {reversal.status}")
        if original.status == "simulated":
            result = _simulate(db, reversal, s)
        else:
            result = _execute(
                db, settings, reversal, s, live=live, confirm=confirm, github=github, config=config
            )
        undone = _get(db, proposal_id)
        if result.mode == "simulated":
            return ExecutionResult(undone, "rolled_back", "simulated proposal rolled back")
        if result.mode == "executed":
            return ExecutionResult(
                undone, "rolled_back", f"re-invited with {target.get('permission')}"
            )
        return ExecutionResult(undone, result.mode, result.detail)


def audit(db: ScopedDB) -> list[str]:
    """Actions without what allowed them: every simulated, executed or rolled back proposal
    needs a finished journal record on a spent grant whose decision approved its exact
    payload, and every rolled back one also a rollback proposal that ran the same way."""
    problems: list[str] = []
    for proposal in proposal_store.list_all(db):
        if proposal.status not in ("simulated", "executed", "rolled_back"):
            continue
        ends = journal.finished(db, proposal.proposal_id)
        if not ends:
            problems.append(
                f"proposal {proposal.proposal_id} is {proposal.status} without a spent grant"
                " and a journal record"
            )
            continue
        grant = grants.get(db, ends[-1].grant_id)
        approval = approvals.get(db, grant.approval_id) if grant is not None else None
        if grant is None or grants.use_of(db, grant.grant_id) is None:
            problems.append(f"proposal {proposal.proposal_id} acted on an unspent grant")
        if not gates.approval_matches(approval, proposal.current_hash()):
            problems.append(
                f"proposal {proposal.proposal_id} is {proposal.status} without approval"
            )
        if proposal.status == "rolled_back" and not any(
            r.status in ("executed", "simulated")
            for r in proposal_store.reversals(db, proposal.proposal_id)
        ):
            problems.append(
                f"proposal {proposal.proposal_id} was rolled back without a rollback approval"
            )
    return problems


def describe(db: ScopedDB, proposal: Proposal, org: str) -> dict[str, Any]:
    call = planned_call(proposal, org)
    item = approvals.for_ref(db, proposal.kind, proposal.proposal_id)
    grant = grants.for_approval(db, item.approval_id) if item is not None else None
    used = grants.use_of(db, grant.grant_id) if grant is not None else None
    return {
        "proposal_id": proposal.proposal_id,
        "verdict_id": proposal.verdict_id,
        "source": proposal.source,
        "reverses": proposal.reverses,
        "action": proposal.action,
        "target": proposal.target,
        "status": proposal.status,
        "risk": proposal.risk,
        "precondition": proposal.precondition,
        "payload_sha256": proposal.payload_sha256,
        "approval": (
            {"approval_id": item.approval_id, "state": item.state, "expires_at": item.expires_at}
            if item is not None
            else None
        ),
        "grant": (
            {
                "grant_id": grant.grant_id,
                "expires_at": grant.expires_at,
                "spent_at": used.used_at if used is not None else None,
            }
            if grant is not None
            else None
        ),
        "journal": [
            {"entry": e.entry, "mode": e.mode, "detail": e.detail, "at": e.recorded_at}
            for e in journal.entries(db, proposal.proposal_id)
        ],
        "planned_call": call.as_dict() if call else None,
        "pre_state": proposal.pre_state,
        "refusal": proposal.refusal,
        "rationale": proposal.rationale,
    }
