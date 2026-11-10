"""Test-only stand-ins for the Copilot's model calls: a keyword classifier, a scripted
gatherer per intent, and an answerer that reads the tool results the graph showed it."""

from __future__ import annotations

import json
import re
from typing import Any

from pydantic import BaseModel

from ccp.evaluators.extraction.base import Cited
from ccp.evaluators.extraction.copilot import CopilotAnswerDraft, IntentDraft, RequestedAction
from ccp.evaluators.extraction.investigation import ProposalTarget
from ccp.llm.client import SystemBlock, ToolCall
from ccp.security.prompt_guard import unfence_tool_result
from tests.support.stub_extractors import blocks

_TEST = re.compile(r"\b((?:HR|GH)-\d)\b")
_QUESTION = re.compile(r"^Question[^:\n]*: (?P<q>.*)$", re.MULTILINE)
_INTENT = re.compile(r"^Intent: (?P<i>\w+)", re.MULTILINE)
_RESULTS = re.compile(r"^TOOL_RESULTS_JSON: (?P<json>.*)$", re.MULTILINE)


def _question(text: str) -> str:
    match = _QUESTION.search(text)
    if not match:
        return ""
    raw = match.group("q").strip()
    return str(json.loads(raw)) if raw.startswith('"') else raw  # quoted since A-98


def classify(schema: type[Any], system: list[SystemBlock], user: str) -> BaseModel:
    question = _question(user)
    lowered = question.lower()
    test = _TEST.search(question)
    if lowered.startswith("fix") or " fix " in f" {lowered} ":
        intent = "act"
    elif "missing" in lowered:
        intent = "missing"
    elif "why" in lowered or "fail" in lowered:
        intent = "explain"
    elif "ready" in lowered or "soc 2" in lowered or "soc2" in lowered or "iso" in lowered:
        intent = "status"
    else:
        intent = "other"
    framework = None
    if "soc 2" in lowered or "soc2" in lowered:
        framework = "soc2"
    elif "iso" in lowered:
        framework = "iso27001"
    return IntentDraft(
        intent=intent,  # type: ignore[arg-type]
        test_id=test.group(1) if test else None,
        subject_hint="Jane Smith" if "jane smith" in lowered else None,
        framework_id=framework,
    )


def _tool_results(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Every tool result the loop has shown so far, parsed, in order."""
    out: list[dict[str, Any]] = []
    for m in messages:
        content = m.get("content")
        if m.get("role") != "user" or not isinstance(content, list):
            continue
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_result":
                try:
                    content = unfence_tool_result(str(block.get("content") or "{}"))
                    out.append(json.loads(content))
                except json.JSONDecodeError:
                    out.append({})
    return out


def gather(
    system: list[SystemBlock], messages: list[dict[str, Any]], tools: list[dict[str, Any]]
) -> tuple[str, list[ToolCall]]:
    """Scripted per intent; the act flow also tries a write tool, which the loop refuses."""
    goal = str(messages[0].get("content", ""))
    intent_match = _INTENT.search(goal)
    intent = intent_match.group("i") if intent_match else "other"
    question = _question(goal).lower()
    step = sum(1 for m in messages if m.get("role") == "assistant")
    results = _tool_results(messages)
    if intent == "status":
        framework = "iso27001" if "iso" in question else "soc2"
        if step == 0:
            return "", [ToolCall("c-1", "get_framework_rollup", {"framework_id": framework})]
        if step == 1:
            return "", [ToolCall("c-2", "list_findings", {"status": "fail"})]
        return "Gathered.", []
    if intent == "other":
        if step == 0:
            query = re.sub(r"[^a-z0-9 ]", "", question)[:80] or "evidence"
            return "", [
                ToolCall(
                    "c-1",
                    "search_evidence",
                    {"query": query, "evidence_type": "access_review_report"},
                )
            ]
        return "Gathered.", []
    test = _TEST.search(_question(goal))  # the question, not the known-tests list
    test_id = test.group(1) if test else "HR-1"
    if step == 0:
        return "", [ToolCall("c-1", "list_findings", {"test_id": test_id})]
    if step == 1:
        findings = ((results[-1] if results else {}).get("result") or {}).get("findings") or []
        chosen = next((f for f in findings if f.get("status") == "fail"), None)
        if chosen is None and findings:
            chosen = findings[0]
        if chosen is None:
            return "No finding for that test.", []
        return "", [ToolCall("c-2", "get_verdict", {"verdict_id": chosen["verdict_id"]})]
    if step == 2:
        verdict = (results[-1] if results else {}).get("result") or {}
        if intent == "missing":
            return "", [
                ToolCall(
                    "c-3", "get_investigation", {"verdict_id": str(verdict.get("verdict_id", ""))}
                )
            ]
        if intent == "act":
            return "", [
                ToolCall(
                    "c-3",
                    "remove_repo_collaborator",
                    {"repo": "acme-example/platform", "login": "jsmith"},
                )
            ]
        return "Gathered.", []
    return "Gathered.", []


def answer(schema: type[Any], system: list[SystemBlock], user: str) -> BaseModel:
    intent_match = _INTENT.search(user)
    intent = intent_match.group("i") if intent_match else "other"
    match = _RESULTS.search(user)
    results: dict[str, Any] = json.loads(match.group("json")) if match else {}
    question = _question(user)
    verdict = next((v for k, v in results.items() if k.startswith("get_verdict:")), None)
    if intent == "explain" and verdict:
        text = (
            f"{verdict['test_id']} is {verdict['status']} for {verdict['subject_key']}:"
            f" {verdict['reasoning']} This verdict came from tier {verdict['tier']}"
            f" ({verdict.get('model') or 'deterministic rules, no model'})."
        )
        cites = [Cited(chunk_id=c["chunk_id"], quote=c["quote"]) for c in verdict["citations"]]
        return CopilotAnswerDraft(text=text, citations=cites, requested_actions=[])
    if intent == "status":  # deliberately breaks the contract so the graph's check is tested
        return CopilotAnswerDraft(
            text="You are ready for SOC 2 and fully compliant. Two requirements have a"
            " failing subject.",
            citations=[],
            requested_actions=[],
        )
    if intent == "missing":
        inv = next((v for k, v in results.items() if k.startswith("get_investigation:")), None)
        if not inv or inv.get("error"):
            text = (
                "No investigation exists for this verdict yet, so I cannot list what is"
                " missing. I can open one if you ask me to."
            )
        else:
            kinds = ", ".join(m["evidence_type"] for m in inv.get("missing_evidence") or [])
            text = f"The investigation lists missing evidence: {kinds or 'nothing'}."
        return CopilotAnswerDraft(text=text, citations=[], requested_actions=[])
    if intent == "act":
        verdict_id = verdict.get("verdict_id") if verdict else None
        actions = [
            RequestedAction(
                action="remove_repo_collaborator",
                target=ProposalTarget(repo="acme-example/platform", login="jsmith"),
                rationale="Jane Smith was terminated and still holds write access on platform.",
                verdict_id=verdict_id,
            )
        ]
        if "everything" in question.lower():
            actions += [
                RequestedAction(
                    action="remove_org_member",
                    target=ProposalTarget(org="acme-example", login="jsmith"),
                    rationale="The org membership survived the termination.",
                    verdict_id=verdict_id,
                ),
                RequestedAction(
                    action="notify_owner",
                    target=ProposalTarget(
                        subject_key=verdict.get("subject_key") if verdict else None
                    ),
                    rationale="Tell the owner.",
                    verdict_id=verdict_id,
                ),
            ]
        return CopilotAnswerDraft(
            text="I have requested the removal of the leftover access; an approval item will"
            " be opened for a reviewer.",
            citations=[],
            requested_actions=actions,
        )
    found = blocks(user)
    seen = "; ".join(b.body.strip().splitlines()[0][:80] for b in found if b.body.strip())
    text = "I have no data for that question." + (f" What I could retrieve: {seen}" if seen else "")
    return CopilotAnswerDraft(text=text, citations=[], requested_actions=[])
