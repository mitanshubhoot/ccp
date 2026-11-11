"""Mapping a new framework: an offline batch of proposals, then one review at a time
(spec.md A-164). `ccp knowledge map propose|review|eval`.

Code ranks the candidates (ccp/knowledge/mapping.py). With a model key a tool-less call
then chooses among those candidates or abstains; a control it names that code did not rank
is dropped, and its own confidence is kept beside code's, never in place of it. The batch
is a file for the reviewer; nothing is mapped until a named reviewer accepts a mapping.
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import typer
import yaml
from pydantic import BaseModel, ConfigDict, Field

from ccp.config import Settings
from ccp.evaluators.extraction import schema_block
from ccp.knowledge import catalogue, mapping
from ccp.knowledge import graph as knowledge
from ccp.knowledge.graph import KNOWLEDGE_DIR, Framework, KnowledgeGraph
from ccp.llm.budget import BudgetTracker, RunBudget
from ccp.llm.client import LLMClient, SystemBlock, try_build_client
from ccp.llm.prompts import load_prompt
from ccp.security import prompt_guard

PROMPT_VERSION = "mapping_v1"
BATCHES = "mapping_batches"


class MappingChoice(BaseModel):
    model_config = ConfigDict(extra="forbid")

    control_ids: list[str] = Field(max_length=3)
    abstain: bool
    confidence: float = Field(ge=0, le=1)
    reason: str = Field(max_length=300)


def refine(
    llm: LLMClient,
    settings: Settings,
    graph: KnowledgeGraph,
    framework: Framework,
    proposal: mapping.Proposal,
) -> mapping.Proposal:
    """The model chooses among code's candidates, or abstains."""
    requirement = framework.requirement(proposal.requirement_id)
    candidates = [
        {
            "control_id": c.control_id,
            "title": graph.controls[c.control_id].title,
            "description": graph.controls[c.control_id].description,
            "covers": list(graph.controls[c.control_id].requirements),
            "score": c.score,
        }
        for c in proposal.candidates
    ]
    nonce = prompt_guard.new_nonce()
    block = prompt_guard.frame_evidence(
        [
            prompt_guard.FramedChunk(
                chunk_id=f"{framework.framework_id}:{requirement.requirement_id}",
                text=mapping.requirement_text(requirement),
                evidence_type="framework_requirement",
            )
        ],
        nonce,
    )
    result = llm.extract(
        schema=MappingChoice,
        system=[
            SystemBlock(load_prompt(PROMPT_VERSION).text),
            SystemBlock(schema_block(MappingChoice), cache=True),
        ],
        user=(
            f"{prompt_guard.fence_declaration(nonce)}\n\nCandidate controls (system data):\n"
            + json.dumps(candidates)
        ),
        model=settings.ccp_mapping_model,
        tracker=BudgetTracker(
            RunBudget.from_settings(settings).lowered(max_seconds=60, max_usd=0.2)
        ),
        max_tokens=512,
        documents=[block.text],
    )
    choice: MappingChoice = result.parsed
    ranked = {c.control_id for c in proposal.candidates}
    kept = tuple(c for c in choice.control_ids if c in ranked)
    events = tuple(f"model_off_candidates:{c}" for c in choice.control_ids if c not in ranked)
    abstained = choice.abstain or not kept
    return replace(
        proposal,
        proposed=() if abstained else kept,
        abstained=abstained,
        method=f"code+model:{result.model}",
        model_reason=f"{choice.reason} (model confidence {choice.confidence:.2f})",
        events=proposal.events + events,
    )


def propose_batch(
    framework_path: Path,
    settings: Settings,
    llm: LLMClient | None = None,
    knowledge_dir: Path = KNOWLEDGE_DIR,
) -> Path:
    graph = knowledge.load(knowledge_dir)
    framework = Framework.model_validate(yaml.safe_load(framework_path.read_text(encoding="utf-8")))
    proposals = mapping.propose(graph, framework)
    if llm is not None:
        proposals = [
            p if p.abstained else refine(llm, settings, graph, framework, p) for p in proposals
        ]
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
    out = knowledge_dir / BATCHES
    out.mkdir(exist_ok=True)
    path = out / f"{framework.framework_id}-{stamp}.json"
    path.write_text(
        json.dumps(
            {
                "framework_id": framework.framework_id,
                "framework_file": str(framework_path),
                "catalogue_version": catalogue.version(knowledge_dir),
                "proposals": [p.as_dict() for p in proposals],
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    return path


map_app = typer.Typer(no_args_is_help=True, help="Map a new framework: proposals, then review")


@map_app.command("propose")
def propose_cmd(
    framework: Path = typer.Option(..., "--framework", help="the framework's YAML file"),
    no_model: bool = typer.Option(False, "--no-model", help="code's ranking only"),
) -> None:
    settings = Settings()
    llm = None if no_model else try_build_client(settings)
    path = propose_batch(framework, settings, llm)
    batch = json.loads(path.read_text(encoding="utf-8"))
    for p in batch["proposals"]:
        shown = "abstained" if p["abstained"] else ", ".join(p["proposed"])
        typer.echo(f"{p['requirement_id']:<10} {p['confidence']:.2f}  {shown}")
    typer.echo(str(path))


@map_app.command("review")
def review_cmd(
    framework: Path = typer.Option(..., "--framework"),
    requirement: str = typer.Option(..., "--requirement"),
    control: str = typer.Option(..., "--control"),
    accept: bool = typer.Option(False, "--accept"),
    reject: bool = typer.Option(False, "--reject"),
    by: str = typer.Option(..., "--by"),
    note: str = typer.Option("", "--note"),
) -> None:
    """One mapping, one decision; an accept makes a new catalogue version."""
    if accept == reject:
        raise typer.BadParameter("pass exactly one of --accept and --reject")
    entry = mapping.review(
        framework_path=framework,
        requirement_id=requirement,
        control_id=control,
        decision="accept" if accept else "reject",
        reviewer=by,
        note=note,
    )
    typer.echo(json.dumps(entry, indent=2))


@map_app.command("eval")
def eval_cmd(framework: str = typer.Option(..., "--held-out")) -> None:
    """Precision and recall at 3 on a framework whose mappings are known."""
    typer.echo(json.dumps(mapping.held_out(knowledge.load(), framework).__dict__, indent=2))
