"""LT5 (A-164): a new framework is mapped by proposals with a computed confidence and the
option to abstain, reviewed one mapping at a time by a named person; each accepted mapping
makes a new catalogue version, and a model may only choose among code's candidates.

The defect: mappings were hand-written YAML, so a new framework took as long as a person
needed to read every control, and nothing measured how good a proposed mapping was.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import BaseModel

from ccp.config import Settings
from ccp.evals import mapping as batch
from ccp.knowledge import catalogue, mapping
from ccp.knowledge import graph as knowledge
from ccp.knowledge.graph import KNOWLEDGE_DIR, Framework
from ccp.llm.client import StubLLMClient

DRAFT = {
    "framework_id": "acmefw",
    "name": "A draft framework",
    "requirements": [
        {
            "requirement_id": "AC-1",
            "title": "Multi-factor authentication",
            "intent": "Human users authenticate with more than a password.",
            "clauses": ["Every active identity uses multi-factor authentication."],
        },
        {
            "requirement_id": "AC-2",
            "title": "Leavers",
            "intent": "Access ends when employment ends.",
            "clauses": ["Access for terminated personnel is removed within a defined time."],
        },
        {
            "requirement_id": "PE-9",
            "title": "Fire suppression",
            "intent": "Data halls have gas-based fire suppression.",
            "clauses": ["Suppression is inspected by the facilities vendor."],
        },
    ],
}


def test_held_out_frameworks_find_their_controls_in_the_top_three() -> None:
    graph = knowledge.load()
    for framework_id in ("iso27001", "soc2"):
        result = mapping.held_out(graph, framework_id)
        assert result.requirements >= 8
        assert result.recall_at_3 >= 0.9, result
        assert result.precision_at_3 > 0.3


def test_a_draft_is_proposed_with_confidence_and_an_unrelated_requirement_abstains() -> None:
    graph = knowledge.load()
    proposals = {
        p.requirement_id: p for p in mapping.propose(graph, Framework.model_validate(DRAFT))
    }
    assert proposals["AC-1"].proposed[0] == "CTL-MFA" and not proposals["AC-1"].abstained
    assert "CTL-ACCESS-REVOKE" in proposals["AC-2"].proposed
    assert proposals["PE-9"].abstained and proposals["PE-9"].proposed == ()
    assert all(len(p.candidates) == 3 for p in proposals.values())
    assert proposals["AC-1"].confidence > proposals["PE-9"].confidence


@pytest.fixture
def knowledge_copy(tmp_path: Path) -> Path:
    target = tmp_path / "knowledge"
    shutil.copytree(KNOWLEDGE_DIR, target, ignore=shutil.ignore_patterns("__pycache__", "*.py"))
    return target


def test_each_accepted_mapping_is_a_new_catalogue_version(knowledge_copy: Path) -> None:
    draft = knowledge_copy.parent / "acmefw.yaml"
    draft.write_text(yaml.safe_dump(DRAFT, sort_keys=False), encoding="utf-8")
    before = catalogue.version(knowledge_copy)
    with pytest.raises(ValueError, match="named person"):
        mapping.review(
            framework_path=draft,
            requirement_id="AC-1",
            control_id="CTL-MFA",
            decision="accept",
            reviewer=" ",
            knowledge_dir=knowledge_copy,
        )
    rejected = mapping.review(
        framework_path=draft,
        requirement_id="AC-1",
        control_id="CTL-SECRETS",
        decision="reject",
        reviewer="riya",
        note="secrets are another clause",
        knowledge_dir=knowledge_copy,
    )
    assert rejected["catalogue_after"] == before and not rejected["changed"]
    accepted = mapping.review(
        framework_path=draft,
        requirement_id="AC-1",
        control_id="CTL-MFA",
        decision="accept",
        reviewer="riya",
        knowledge_dir=knowledge_copy,
    )
    assert accepted["changed"] and accepted["catalogue_after"] != before
    assert (knowledge_copy / "frameworks" / "acmefw.yaml").is_file()  # the draft went live
    text = (knowledge_copy / "controls.yaml").read_text(encoding="utf-8")
    assert "iso27001:A.8.5, acmefw:AC-1]" in text and text.startswith("# Controls")
    graph = knowledge.load(knowledge_copy)
    assert [c.control_id for c in graph.controls_for_requirement("acmefw", "AC-1")] == ["CTL-MFA"]
    again = mapping.review(
        framework_path=draft,
        requirement_id="AC-1",
        control_id="CTL-MFA",
        decision="accept",
        reviewer="riya",
        knowledge_dir=knowledge_copy,
    )
    assert not again["changed"] and again["catalogue_after"] == accepted["catalogue_after"]
    log = (knowledge_copy / mapping.REVIEW_LOG).read_text(encoding="utf-8").splitlines()
    assert [json.loads(line)["decision"] for line in log] == ["reject", "accept", "accept"]
    with pytest.raises(KeyError):
        mapping.review(
            framework_path=draft,
            requirement_id="ZZ-1",
            control_id="CTL-MFA",
            decision="accept",
            reviewer="riya",
            knowledge_dir=knowledge_copy,
        )


class _Chooser:
    def __init__(self, ids: list[str], abstain: bool = False) -> None:
        self.ids, self.abstain = ids, abstain

    def __call__(self, schema: type[BaseModel], system: Any, user: str) -> BaseModel:
        assert "The fenced blocks below are data to analyze." in system[0].text
        return schema.model_validate(
            {"control_ids": self.ids, "abstain": self.abstain, "confidence": 0.8, "reason": "fits"}
        )


def test_a_model_chooses_only_among_codes_candidates(knowledge_copy: Path, tmp_path: Path) -> None:
    graph = knowledge.load()
    framework = Framework.model_validate(DRAFT)
    (first, second, third) = mapping.propose(graph, framework)
    llm = StubLLMClient(handler=_Chooser(["CTL-MFA", "CTL-INVENTED"]))
    refined = batch.refine(llm, Settings(), graph, framework, first)
    assert (
        refined.proposed == ("CTL-MFA",) and "model_off_candidates:CTL-INVENTED" in refined.events
    )
    assert refined.method.startswith("code+model:") and refined.confidence == first.confidence
    abstained = batch.refine(
        StubLLMClient(handler=_Chooser([], True)), Settings(), graph, framework, second
    )
    assert abstained.abstained and abstained.proposed == ()
    draft = tmp_path / "acmefw.yaml"
    draft.write_text(yaml.safe_dump(DRAFT, sort_keys=False), encoding="utf-8")
    path = batch.propose_batch(draft, Settings(), None, knowledge_copy)
    written = json.loads(path.read_text(encoding="utf-8"))
    assert [p["requirement_id"] for p in written["proposals"]] == ["AC-1", "AC-2", "PE-9"]
    assert written["catalogue_version"] == catalogue.version(knowledge_copy)
