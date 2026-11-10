"""Tier 2: one hybrid retrieval call plus one extraction call, then code decides
(spec.md Layer 7 "Tier 2", R5).

    scope ──► retrieve ──► frame (nonce fence, redaction, marker scan) ──► extract (schema)
          ──► verify citations ──► rule ──► computed confidence ──► Verdict

The model call runs outside any transaction; every step is logged; the exact request and
response are stored on the verdict for replay. Models extract, code decides.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from ccp.config import Settings
from ccp.evaluators import budget_gate, citations, confidence, registry, rules
from ccp.evaluators.context import EvaluationContext
from ccp.evaluators.deterministic.common import hr_person_for
from ccp.evaluators.extraction import schema_block, schema_for
from ccp.evaluators.verdict import TriggerReason, Verdict, evaluation_key
from ccp.knowledge.graph import TestSpec
from ccp.llm.budget import BudgetTracker, RunBudget
from ccp.llm.client import LLMClient, SystemBlock, try_build_client
from ccp.llm.prompts import load_prompt
from ccp.observability import steps
from ccp.retrieval import config as retrieval_config
from ccp.retrieval import hybrid
from ccp.retrieval.chunker import CHUNKER_VERSION
from ccp.retrieval.embeddings import EmbeddingClient, build_embedding_client
from ccp.retrieval.rerank import Reranker, build_reranker
from ccp.security import prompt_guard
from ccp.security.prompt_guard import FramedChunk
from ccp.storage.scoped import ScopedDB
from ccp.types import EmploymentStatus, EvidenceType, VerdictStatus


@dataclass
class Providers:
    """Everything the AI tiers need from outside the database."""

    embedder: EmbeddingClient
    reranker: Reranker | None
    llm: LLMClient | None
    retrieval: retrieval_config.RetrievalConfig

    @classmethod
    def from_settings(cls, settings: Settings, llm: LLMClient | None = None) -> Providers:
        return cls(
            embedder=build_embedding_client(settings),
            reranker=build_reranker(settings),
            llm=llm if llm is not None else try_build_client(settings),
            retrieval=retrieval_config.load(),
        )


class AITierUnavailable(RuntimeError):
    """No model client is configured (ANTHROPIC_API_KEY unset); the record is parked."""


# -- subjects ------------------------------------------------------------------------------


def subjects_for(ctx: EvaluationContext, spec: TestSpec) -> list[str]:
    if spec.test_id == "GH-4":
        repos = sorted(
            {
                e.subject_entity_id
                for e in ctx.evidence
                if e.evidence_type is EvidenceType.policy_document
                and e.subject_entity_id
                and e.subject_entity_id.startswith("github:asset:")
            }
        )
        out = [f"repo:{r}" for r in repos]
        if any(
            e.evidence_type is EvidenceType.policy_document and not e.subject_entity_id
            for e in ctx.evidence
        ):
            out.append("tenant")
        return out
    if spec.test_id == "HR-3":
        return sorted(
            f"person:{ctx.canonical_of(p.entity_id)}"
            for p in ctx.persons.values()
            if p.entity_id.startswith("hris:person:")
            and p.employment_status is EmploymentStatus.active
        )
    return ["tenant"] if spec.subject_kind == "tenant" else []


def subject_entity(subject: str) -> str | None:
    if subject == "tenant":
        return None
    return subject.split(":", 1)[1]


def extraction_instruction(nonce: str, description: str) -> str:
    """The instruction text of a tier 2 call. The fenced evidence travels beside it as a
    document block, never inside it (A-98); replay rebuilds the call with this too."""
    return (
        f"{prompt_guard.fence_declaration(nonce)}\n\n{description}\n\n"
        "Fill every field of the schema from the fenced evidence in the documents only."
    )


def subject_names(ctx: EvaluationContext, subject: str) -> tuple[str, ...]:
    """What a quote must name to show the document is the subject's (A-98)."""
    entity = subject_entity(subject)
    if subject.startswith("person:") and entity:
        person = hr_person_for(ctx, entity)
        if person is not None:
            return tuple(n for n in (person.display_name, person.employee_id) if n)
    return ()


def build_query(ctx: EvaluationContext, spec: TestSpec, subject: str) -> tuple[str, str]:
    """(retrieval query, a one-line subject description for the prompt)."""
    template = str(spec.parameters.get("retrieval_query") or spec.title)
    entity = subject_entity(subject)
    display_name, employee_id = "", ""
    if subject.startswith("person:") and entity:
        person = hr_person_for(ctx, entity)
        if person is not None:
            display_name = person.display_name or ""
            employee_id = person.employee_id or ""
        description = f"Employee: {display_name or entity} (employee id {employee_id or 'unknown'})"
    elif subject.startswith("repo:") and entity:
        asset = ctx.assets.get(entity)
        description = f"Repository: {asset.name if asset else entity}"
    else:
        description = f"Scope: the whole tenant {ctx.tenant.settings.display_name}"
    query = template.format(display_name=display_name, employee_id=employee_id, subject=subject)
    return query.strip(), description


# -- the evaluation -------------------------------------------------------------------------


def _schema_block(meta_model: type[Any]) -> str:
    return schema_block(meta_model)


def evaluate(
    db: ScopedDB,
    settings: Settings,
    ctx: EvaluationContext,
    spec: TestSpec,
    subject: str,
    providers: Providers,
    *,
    reason: TriggerReason = "impact",
) -> Verdict:
    if providers.llm is None:
        raise AITierUnavailable("ANTHROPIC_API_KEY is not set; tier 2 evaluation is unavailable")
    assert spec.prompt_version and spec.extraction_schema
    meta = schema_for(spec.extraction_schema)
    prompt = load_prompt(spec.prompt_version)
    model = settings.ccp_tier2_model
    subject_key = f"{spec.test_id}|{subject}"
    key = evaluation_key(
        ctx.tenant.tenant_id,
        spec.test_id,
        subject_key,
        ctx.trigger_observed_at,
        spec.prompt_version,
        model,
    )
    evidence_type = spec.evidence_types[0].value
    query, description = build_query(ctx, spec, subject)
    entity = subject_entity(subject)

    # 1. retrieve
    with steps.step(
        "tier2.retrieve",
        layer="evaluators",
        tenant=ctx.tenant,
        inputs={
            "test_id": spec.test_id,
            "subject": subject,
            "query": query,
            "evidence_type": evidence_type,
        },
    ) as s:
        result = hybrid.retrieve(
            db,
            providers.embedder,
            query,
            evidence_type=evidence_type,
            subject_entity_id=entity,
            reranker=providers.reranker,
            config=providers.retrieval,
        )
        for event in result.events:
            s.event(event)
        s.set_output(
            {
                "embed_model": result.embed_model,
                "rerank_model": result.rerank_model,
                "candidates": [c.scores() for c in result.candidates],
                "chunk_ids": [c.chunk_id for c in result.chunks],
            }
        )
    events: list[str] = list(result.events)

    if not result.chunks:
        conf = confidence.compute(
            coverage=0.0,
            citation_rate=1.0,
            retrieval_quality=None,
            no_evidence=True,
            provider_degraded=result.degraded,
        )
        return Verdict(
            evaluation_key=key,
            tenant_id=ctx.tenant.tenant_id,
            test_id=spec.test_id,
            subject_key=subject_key,
            status=VerdictStatus.needs_review,
            reasoning=f"no {evidence_type} evidence retrieved for {description}; {conf.summary()}",
            confidence=conf.value,
            confidence_cap=conf.cap,
            tier=2,
            model=None,  # no model answered
            prompt_version=spec.prompt_version,
            guardrail_events=tuple(events + conf.events + ["no_evidence_retrieved"]),
            chunker_version=CHUNKER_VERSION,
            schema_version=meta.version,
            trigger_observed_at=ctx.trigger_observed_at,
            trigger_reason=reason,
            valid_from=ctx.trigger_observed_at,
            created_at=ctx.now,
        )

    # 2. frame
    nonce = prompt_guard.new_nonce()
    framed = prompt_guard.frame_evidence(
        [
            FramedChunk(
                c.chunk_id, c.text, c.evidence_type, c.subject_entity_id, c.page, c.source_ref
            )
            for c in result.chunks
        ],
        nonce,
    )
    events += framed.events
    shown = {c.chunk_id: c for c in result.chunks if c.chunk_id in framed.shown_chunk_ids}
    if not shown:
        conf = confidence.compute(
            coverage=0.0, citation_rate=1.0, retrieval_quality=None, no_evidence=True
        )
        return Verdict(
            evaluation_key=key,
            tenant_id=ctx.tenant.tenant_id,
            test_id=spec.test_id,
            subject_key=subject_key,
            status=VerdictStatus.needs_review,
            reasoning="every retrieved chunk was rejected by the evidence fence",
            confidence=conf.value,
            confidence_cap=conf.cap,
            tier=2,
            model=None,  # no model answered
            prompt_version=spec.prompt_version,
            retrieved_chunk_ids=tuple(c.chunk_id for c in result.chunks),
            guardrail_events=tuple(events + conf.events),
            chunker_version=CHUNKER_VERSION,
            schema_version=meta.version,
            fence_nonce=nonce,
            trigger_observed_at=ctx.trigger_observed_at,
            trigger_reason=reason,
            valid_from=ctx.trigger_observed_at,
            created_at=ctx.now,
        )
    system = [SystemBlock(prompt.text), SystemBlock(_schema_block(meta.model), cache=True)]
    user = extraction_instruction(nonce, description)  # the evidence goes beside it

    # 3. extract (no transaction is open here)
    tracker = BudgetTracker(RunBudget.from_settings(settings))
    with steps.step(
        "tier2.extract",
        layer="evaluators",
        kind="generation",
        tenant=ctx.tenant,
        inputs={
            "test_id": spec.test_id,
            "subject": subject,
            "prompt_sha256": prompt.sha256,
            "chunk_ids": sorted(shown),
            "fence_nonce": nonce,
        },
    ) as s:
        s.model(model, prompt_version=spec.prompt_version, prompt_sha256=prompt.sha256)
        extraction = providers.llm.extract(
            schema=meta.model,
            system=system,
            user=user,
            model=model,
            tracker=tracker,
            documents=[framed.text],
        )
        s.usage(
            input_tokens=extraction.usage.input_tokens,
            output_tokens=extraction.usage.output_tokens,
            cache_read=extraction.usage.cache_read_input_tokens,
            cache_write=extraction.usage.cache_creation_input_tokens,
        )
        budget_gate.record_spend(
            db,
            extraction.usage.usd,
            extraction.usage.input_tokens + extraction.usage.output_tokens,
            s.step_id,
        )
        s.set_output(
            {"usd": extraction.usage.usd, "fields": extraction.parsed.model_dump(mode="json")}
        )

    # 4. verify, 5. rule, 6. confidence
    verified = citations.verify_fields(
        extraction.parsed,
        {cid: c.text for cid, c in shown.items()},
        meta,
        subject_names=subject_names(ctx, subject),
    )
    events += verified.events
    params = dict(spec.parameters)
    params.setdefault("evidence_max_age_days", spec.evidence_max_age_days)
    decision = rules.decide(spec.rule, verified.values, params, ctx.now)
    top = result.chunks[0]
    quality = retrieval_config.quality_from_thresholds(
        top.top_score, providers.retrieval.thresholds_for(result.embed_model, result.rerank_model)
    )
    conf = confidence.compute(
        coverage=verified.coverage,
        citation_rate=verified.citation_rate,
        retrieval_quality=quality,
        injection=framed.injection_detected,
        provider_degraded=result.degraded,
    )
    events += conf.events
    status = conf.forced_status or decision.status
    valid_from = ctx.valid_from(evidence_observed_at=_newest_observed(ctx, shown))
    reasoning = "; ".join(decision.reasons) + f". {description}. {conf.summary()}"
    return Verdict(
        evaluation_key=key,
        tenant_id=ctx.tenant.tenant_id,
        test_id=spec.test_id,
        subject_key=subject_key,
        status=status,
        reasoning=reasoning,
        citations=tuple(verified.citations),
        evidence_refs=tuple(sorted({c.evidence_entity_id for c in shown.values()})),
        confidence=conf.value,
        confidence_cap=conf.cap,
        model_confidence=None,
        tier=2,
        model=extraction.model,
        prompt_version=spec.prompt_version,
        retrieved_chunk_ids=tuple(c.chunk_id for c in result.chunks),
        guardrail_events=tuple(events),
        extracted_fields=verified.values,
        chunker_version=CHUNKER_VERSION,
        schema_version=meta.version,
        fence_nonce=nonce,
        trigger_observed_at=ctx.trigger_observed_at,
        trigger_reason=reason,
        valid_from=valid_from,
        request_json=extraction.request,
        response_json=extraction.response,
        created_at=ctx.now,
    )


def _newest_observed(
    ctx: EvaluationContext, shown: dict[str, hybrid.RetrievedChunk]
) -> datetime | None:
    ids = {c.evidence_entity_id for c in shown.values()}
    stamps = [e.observed_at for e in ctx.evidence if e.entity_id in ids]
    return max(stamps) if stamps else None


registry.register_tier(2, evaluate)
