"""What an evaluator sees: the tenant's current entities, identity links, review state,
the test spec, the trigger, and an injectable clock. Loaded once per evaluation."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from ccp.config import Settings
from ccp.evaluators import completeness
from ccp.evaluators.rule_versions import rule_stamp
from ccp.evaluators.verdict import TriggerReason, Verdict, evaluation_key
from ccp.identity import resolver
from ccp.ingest import sync_runs
from ccp.ingest.sync_runs import ScopeState
from ccp.knowledge.graph import TestSpec
from ccp.normalize import repository
from ccp.normalize.entities import AccessGrant, Asset, Evidence, Person
from ccp.storage.scoped import ScopedDB
from ccp.tenancy import TenantContext
from ccp.types import VerdictStatus


@dataclass
class EvaluationContext:
    tenant: TenantContext
    settings: Settings
    spec: TestSpec
    trigger_observed_at: datetime
    now: datetime
    persons: dict[str, Person]
    grants: list[AccessGrant]
    assets: dict[str, Asset]
    evidence: list[Evidence]
    canonical: dict[str, str]
    pending_review: set[str]
    reason: TriggerReason = "impact"
    sources: dict[str, ScopeState] = field(default_factory=dict)  # latest pulls (A-123)
    _groups: dict[str, set[str]] = field(default_factory=dict, repr=False)

    def canonical_of(self, entity_id: str) -> str:
        return self.canonical.get(entity_id, entity_id)

    def group(self, canonical_id: str) -> set[str]:
        """Every person entity id that resolves to this canonical id (including itself)."""
        if not self._groups:
            for entity_id, canonical in self.canonical.items():
                self._groups.setdefault(canonical, set()).add(entity_id)
        return self._groups.get(canonical_id, set()) | {canonical_id}

    def grants_for(self, canonical_id: str) -> list[AccessGrant]:
        ids = self.group(canonical_id)
        return sorted(
            (g for g in self.grants if g.person_entity_id in ids), key=lambda g: g.entity_id
        )

    def is_pending_review(self, canonical_id: str) -> bool:
        return bool(self.group(canonical_id) & self.pending_review)

    def sources_digest(self) -> str:
        """The required sources' states, part of the evaluation key (A-123)."""
        stale_after = timedelta(hours=self.settings.ccp_sync_stale_hours)
        return completeness.digest(self.spec, self.sources, self.now, stale_after)

    @property
    def sla(self) -> timedelta:
        key = str(self.spec.parameters.get("sla_hours_key", "termination_revocation_sla_hours"))
        return timedelta(hours=getattr(self.tenant.settings, key))

    def valid_from(
        self, *, evidence_observed_at: datetime | None = None, event_time: datetime | None = None
    ) -> datetime:
        strategy = self.spec.valid_from_strategy
        if strategy == "evidence_observed" and evidence_observed_at is not None:
            return evidence_observed_at
        if strategy == "event_time" and event_time is not None:
            return event_time
        if strategy == "event_time_plus_sla" and event_time is not None:
            return event_time + self.sla
        return self.trigger_observed_at

    def verdict(
        self,
        subject_key: str,
        status: VerdictStatus,
        reasoning: str,
        evidence_refs: list[str],
        valid_from: datetime,
        guardrail_events: tuple[str, ...] = (),
    ) -> Verdict:
        """A deterministic-tier verdict: confidence 1.0, no model, no citations."""
        return Verdict(
            evaluation_key=evaluation_key(
                self.tenant.tenant_id,
                self.spec.test_id,
                subject_key,
                self.trigger_observed_at,
                None,
                None,
                rule=rule_stamp(self.spec, self.tenant.settings),
                sources=self.sources_digest(),
            ),
            tenant_id=self.tenant.tenant_id,
            test_id=self.spec.test_id,
            subject_key=subject_key,
            status=status,
            reasoning=reasoning,
            evidence_refs=tuple(sorted(set(evidence_refs))),
            confidence=1.0,
            confidence_cap=1.0,
            tier=1,
            guardrail_events=guardrail_events,
            trigger_observed_at=self.trigger_observed_at,
            trigger_reason=self.reason,
            valid_from=valid_from,
            created_at=self.now,
            rule_version=rule_stamp(self.spec, self.tenant.settings),
        )


def load_context(
    db: ScopedDB,
    settings: Settings,
    spec: TestSpec,
    trigger_observed_at: datetime,
    now: datetime | None = None,
    reason: TriggerReason = "impact",
) -> EvaluationContext:
    return EvaluationContext(
        tenant=db.tenant,
        settings=settings,
        spec=spec,
        trigger_observed_at=trigger_observed_at,
        now=now or datetime.now(UTC),
        persons={p.entity_id: p for p in repository.current_persons(db)},
        grants=repository.current_grants(db),
        assets={a.entity_id: a for a in repository.current_assets(db)},
        evidence=repository.current_evidence(db),
        canonical=resolver.canonical_map(db),
        pending_review=resolver.pending_review_entities(db),
        reason=reason,
        sources=sync_runs.latest_scopes(db),
    )
