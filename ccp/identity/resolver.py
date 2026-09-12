"""The resolver: candidate pairs -> decisions -> links or review items, all logged.

Re-running is idempotent: a pair whose latest decision compared identical features is
skipped, so the log grows only when something about the pair changed.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from ulid import ULID

from ccp.identity.blocking import CandidatePair, candidate_pairs
from ccp.identity.config import IdentityConfig
from ccp.identity.scoring import Scored, evaluate
from ccp.normalize import repository
from ccp.normalize.entities import Person
from ccp.observability import steps
from ccp.storage.scoped import ScopedDB


@dataclass
class ResolutionSummary:
    compared: int = 0
    skipped_unchanged: int = 0
    routes: dict[str, int] = field(default_factory=dict)

    def add(self, route: str) -> None:
        self.routes[route] = self.routes.get(route, 0) + 1


def _pair_fingerprints(db: ScopedDB) -> dict[tuple[str, str], str]:
    rows = db.fetch_all(
        "SELECT DISTINCT ON (left_entity_id, right_entity_id) left_entity_id, right_entity_id,"
        " features, gate_result FROM resolution_decisions"
        " ORDER BY left_entity_id, right_entity_id, recorded_at DESC"
    )
    out: dict[tuple[str, str], str] = {}
    for left, right, feats, gate_result in rows:
        loaded = json.loads(feats) if isinstance(feats, str) else feats
        out[(left, right)] = json.dumps({"gate": gate_result, "features": loaded}, sort_keys=True)
    return out


def _canonical_for(db: ScopedDB, entity_id: str) -> str | None:
    row = db.fetch_one(
        "SELECT canonical_person_id FROM current_person_links WHERE person_entity_id = %s",
        (entity_id,),
    )
    return str(row[0]) if row else None


def _choose_canonical(db: ScopedDB, left: Person, right: Person) -> str:
    for candidate in (left, right):
        existing = _canonical_for(db, candidate.entity_id)
        if existing:
            return existing
    hris = [p for p in (left, right) if p.entity_id.startswith("hris:person:")]
    return hris[0].entity_id if hris else min(left.entity_id, right.entity_id)


def record_decision(
    db: ScopedDB, pair: CandidatePair, scored: Scored, config: IdentityConfig
) -> str:
    decision_id = str(ULID())
    thresholds = {
        "weights": config.weights.model_dump(),
        "thresholds": config.thresholds.model_dump(),
    }
    with db.transaction() as cur:
        cur.execute(
            "INSERT INTO resolution_decisions (decision_id, tenant_id, left_entity_id,"
            " right_entity_id, block_keys, gate_result, features, score, route, thresholds)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (
                decision_id,
                db.tenant.tenant_id,
                pair.left.entity_id,
                pair.right.entity_id,
                json.dumps(list(pair.keys)),
                scored.gate_result,
                json.dumps(scored.features),
                scored.score,
                scored.route,
                json.dumps(thresholds),
            ),
        )
        if scored.route == "auto_merge":
            canonical = _choose_canonical(db, pair.left, pair.right)
            for person in (pair.left, pair.right):
                cur.execute(
                    "INSERT INTO person_links (link_id, tenant_id, person_entity_id,"
                    " canonical_person_id, decided_by, decision_id)"
                    " VALUES (%s, %s, %s, %s, %s, %s)",
                    (
                        str(ULID()),
                        db.tenant.tenant_id,
                        person.entity_id,
                        canonical,
                        "auto_merge",
                        decision_id,
                    ),
                )
        elif scored.route == "review":
            cur.execute(
                "INSERT INTO review_queue (decision_id, tenant_id) VALUES (%s, %s)",
                (decision_id, db.tenant.tenant_id),
            )
    return decision_id


def resolve_tenant(db: ScopedDB, config: IdentityConfig | None = None) -> ResolutionSummary:
    cfg = config or IdentityConfig.load()
    persons = repository.current_persons(db)
    fingerprints = _pair_fingerprints(db)
    summary = ResolutionSummary()
    with steps.step("identity.resolve", layer="identity", tenant=db.tenant) as s:
        for pair in candidate_pairs(persons):
            scored = evaluate(pair.left, pair.right, cfg)
            fingerprint = json.dumps(
                {"gate": scored.gate_result, "features": scored.features}, sort_keys=True
            )
            if fingerprints.get((pair.left.entity_id, pair.right.entity_id)) == fingerprint:
                summary.skipped_unchanged += 1
                continue
            record_decision(db, pair, scored, cfg)
            summary.compared += 1
            summary.add(scored.route)
            if scored.route == "blocked":
                s.event(f"{scored.gate_result}:{pair.left.entity_id}|{pair.right.entity_id}")
        s.set_output({"compared": summary.compared, "routes": summary.routes})
    return summary


# -- reads used by evaluators and the CLI ---------------------------------------------


def canonical_map(db: ScopedDB) -> dict[str, str]:
    """person_entity_id -> canonical_person_id for every linked person."""
    rows = db.fetch_all("SELECT person_entity_id, canonical_person_id FROM current_person_links")
    return {str(r[0]): str(r[1]) for r in rows}


def canonical_id(db: ScopedDB, entity_id: str) -> str:
    return _canonical_for(db, entity_id) or entity_id


def pending_review_entities(db: ScopedDB) -> set[str]:
    """Entity ids that appear in an open review item; tests involving them report needs_review."""
    rows = db.fetch_all(
        "SELECT d.left_entity_id, d.right_entity_id FROM open_review_queue q"
        " JOIN resolution_decisions d ON d.decision_id = q.decision_id"
    )
    out: set[str] = set()
    for left, right in rows:
        out.add(str(left))
        out.add(str(right))
    return out


@dataclass(frozen=True)
class ReviewItem:
    decision_id: str
    left_entity_id: str
    right_entity_id: str
    score: float | None
    features: dict[str, Any]
    opened_at: datetime


def open_reviews(db: ScopedDB) -> list[ReviewItem]:
    rows = db.fetch_all(
        "SELECT q.decision_id, d.left_entity_id, d.right_entity_id, d.score, d.features,"
        " q.opened_at FROM open_review_queue q"
        " JOIN resolution_decisions d ON d.decision_id = q.decision_id"
        " ORDER BY q.opened_at"
    )
    return [
        ReviewItem(
            decision_id=str(r[0]),
            left_entity_id=str(r[1]),
            right_entity_id=str(r[2]),
            score=r[3],
            features=json.loads(r[4]) if isinstance(r[4], str) else dict(r[4]),
            opened_at=r[5],
        )
        for r in rows
    ]


def resolve_review(
    db: ScopedDB, decision_id: str, outcome: str, reviewer: str, note: str | None = None
) -> None:
    if outcome not in ("merge", "distinct"):
        raise ValueError("outcome must be merge or distinct")
    if not reviewer.strip():
        raise ValueError("a reviewer name is required")
    items = {i.decision_id: i for i in open_reviews(db)}
    item = items.get(decision_id)
    if item is None:
        raise LookupError(f"no open review item {decision_id!r}")
    persons = {p.entity_id: p for p in repository.current_persons(db)}
    with db.transaction() as cur:
        cur.execute(
            "INSERT INTO review_resolutions (resolution_id, decision_id, tenant_id, outcome,"
            " reviewer, note) VALUES (%s, %s, %s, %s, %s, %s)",
            (str(ULID()), decision_id, db.tenant.tenant_id, outcome, reviewer, note),
        )
        if outcome == "merge":
            left, right = persons[item.left_entity_id], persons[item.right_entity_id]
            canonical = _choose_canonical(db, left, right)
            for person in (left, right):
                cur.execute(
                    "INSERT INTO person_links (link_id, tenant_id, person_entity_id,"
                    " canonical_person_id, decided_by, decision_id)"
                    " VALUES (%s, %s, %s, %s, %s, %s)",
                    (
                        str(ULID()),
                        db.tenant.tenant_id,
                        person.entity_id,
                        canonical,
                        "human",
                        decision_id,
                    ),
                )
