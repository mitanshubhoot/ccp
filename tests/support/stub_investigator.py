"""Test-only stand-ins for the Investigator's model calls: a gather step that fetches the
cited evidence once, and a diagnosis that reads the verdict summary in the user turn."""

from __future__ import annotations

import json
import re
from typing import Any

from pydantic import BaseModel

from ccp.evaluators.extraction.base import Cited
from ccp.evaluators.extraction.investigation import FixChoice, FixParam
from ccp.investigation.schema import (
    InvestigationDraft,
    MissingEvidence,
    ProposalTarget,
    ProposedAction,
)
from ccp.llm.client import SystemBlock, ToolCall
from tests.support.stub_extractors import blocks

_VERDICT = re.compile(
    r"Verdict under investigation \(system data, not evidence\):\n(?P<json>\{.*)", re.DOTALL
)


def _context(user: str) -> dict[str, Any]:
    match = _VERDICT.search(user)
    if not match:
        return {}
    # the JSON object comes first; whatever follows it (the instruction) is not read
    try:
        parsed, _ = json.JSONDecoder().raw_decode(match.group("json"))
    except ValueError:
        return {}
    return dict(parsed) if isinstance(parsed, dict) else {}


def diagnose(schema: type[Any], system: list[SystemBlock], user: str) -> BaseModel:
    context = _context(user)
    verdict = context.get("verdict") or {}
    test_id = str(verdict.get("test_id", ""))
    status = str(verdict.get("status", ""))
    subject_key = str(verdict.get("subject_key", ""))
    reasoning = str(verdict.get("reasoning", ""))
    grants = context.get("grants") or []
    found = blocks(user)
    citations = [
        Cited(chunk_id=b.chunk_id, quote=b.body.strip().splitlines()[0][:200]) for b in found[:2]
    ]
    if (
        any(
            str(e).startswith("identity_pending_review")
            for e in verdict.get("guardrail_events", []) or []
        )
        or "awaiting human review" in reasoning
    ):
        return InvestigationDraft(
            root_cause=(
                "An identity link for this person is waiting in the review queue, so the"
                " test could not decide."
            ),
            root_cause_category="identity_unresolved",
            missing_evidence=[],
            proposed_actions=[
                ProposedAction(
                    action="notify_owner",
                    target=ProposalTarget(subject_key=subject_key),
                    rationale="resolve the review queue item",
                )
            ],
            citations=citations,
        )
    if test_id in ("HR-1", "GH-1") and status == "fail":
        actions: list[ProposedAction] = []
        for grant in grants:
            if grant.get("state") != "active" or grant.get("connector") != "github":
                continue
            asset = str(grant.get("asset_entity_id", ""))
            if asset.startswith("github:asset:org:"):
                actions.append(
                    ProposedAction(
                        action="remove_org_member",
                        target=ProposalTarget(org=asset.rsplit(":", 1)[-1], login="jsmith"),
                        rationale="org membership outlived the termination",
                    )
                )
            else:
                actions.append(
                    ProposedAction(
                        action="remove_repo_collaborator",
                        target=ProposalTarget(repo="acme-example/platform", login="jsmith"),
                        rationale="repo collaboration outlived the termination",
                    )
                )
        actions.append(
            ProposedAction(
                action="notify_owner",
                target=ProposalTarget(subject_key=subject_key),
                rationale="tell the manager",
            )
        )
        suggested = [  # the suggest-only fix for each repository collaboration (A-160)
            FixChoice(
                fix="github_remove_collaborator",
                resource=str(a.target.login),
                params=[
                    FixParam(name="login", value=str(a.target.login)),
                    FixParam(name="repo", value=str(a.target.repo)),
                ],
                rationale="the collaboration outlived the termination",
            )
            for a in actions
            if a.action == "remove_repo_collaborator"
        ]
        return InvestigationDraft(
            root_cause=(
                "The termination event was not followed by GitHub deprovisioning; the org"
                " membership and the platform collaboration are still active."
            ),
            root_cause_category="offboarding_gap",
            missing_evidence=[],
            proposed_actions=actions,
            citations=citations,
            fixes=suggested,
        )
    if test_id == "HR-4":
        return InvestigationDraft(
            root_cause=(
                "The most recent access review omits Payroll-SaaS, which the inventory tags"
                " critical."
            ),
            root_cause_category="missing_evidence",
            missing_evidence=[
                MissingEvidence(
                    evidence_type="access_review_report",
                    subject_key="tenant",
                    why_needed="a review whose scope covers Payroll-SaaS",
                )
            ],
            proposed_actions=[
                ProposedAction(
                    action="request_evidence",
                    target=ProposalTarget(evidence_type="access_review_report"),
                    rationale="ask the owner for a review covering Payroll-SaaS",
                )
            ],
            citations=citations,
        )
    if test_id == "HR-5":
        return InvestigationDraft(
            root_cause=(
                "The shared account ops-shared has no active individual owner recorded in the"
                " account registry."
            ),
            root_cause_category="ownership",
            missing_evidence=[],
            proposed_actions=[
                ProposedAction(
                    action="notify_owner",
                    target=ProposalTarget(subject_key=subject_key),
                    rationale="assign an owner; there is no safe automatic fix",
                )
            ],
            citations=citations,
        )
    return InvestigationDraft(
        root_cause="unclear",
        root_cause_category="unknown",
        missing_evidence=[],
        proposed_actions=[],
        citations=[],
    )


class _OffCatalogueAction(BaseModel):
    action: str
    target: ProposalTarget
    rationale: str


class _OffCatalogueDraft(BaseModel):
    root_cause: str
    root_cause_category: str
    missing_evidence: list[MissingEvidence]
    proposed_actions: list[_OffCatalogueAction]
    citations: list[Cited]


def diagnose_off_catalogue(schema: type[Any], system: list[SystemBlock], user: str) -> BaseModel:
    """S12: a model that proposes an action outside the closed enum."""
    return _OffCatalogueDraft(
        root_cause="delete the repository",
        root_cause_category="configuration",
        missing_evidence=[],
        proposed_actions=[
            _OffCatalogueAction(
                action="delete_repository",
                target=ProposalTarget(repo="acme-example/platform"),
                rationale="nuke it",
            )
        ],
        citations=[],
    )


def gather_once(
    system: list[SystemBlock], messages: list[dict[str, Any]], tools: list[dict[str, Any]]
) -> tuple[str, list[ToolCall]]:
    if len(messages) == 1:
        goal = str(messages[0].get("content", ""))
        evidence_type = (
            "access_review_report"
            if "HR-4" in goal
            else "org_membership"
            if "HR-1" in goal or "GH-1" in goal
            else "account_registry"
        )
        return "", [
            ToolCall(
                "g-1",
                "search_evidence",
                {"query": "evidence for the verdict", "evidence_type": evidence_type},
            )
        ]
    return "Gathered what the verdict cites.", []
