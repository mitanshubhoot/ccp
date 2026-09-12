"""The reader (spec.md Layer 9): current, believed, and as-of state, the chain, and the
framework rollup. Reports, remediation, the Copilot, and the eval harness read here."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from ccp.knowledge import graph as knowledge
from ccp.knowledge import rollup as knowledge_rollup
from ccp.knowledge.rollup import FrameworkRollup
from ccp.storage import bitemporal
from ccp.storage.bitemporal import StateRow
from ccp.storage.scoped import ScopedDB


def current_state(db: ScopedDB, subject_key: str) -> StateRow | None:
    return bitemporal.current_state(db, subject_key)


def believed_state(db: ScopedDB, subject_key: str, at: datetime) -> StateRow | None:
    return bitemporal.believed_state(db, subject_key, at)


def state_as_of(
    db: ScopedDB, subject_key: str, valid_at: datetime, known_at: datetime
) -> StateRow | None:
    return bitemporal.state_as_of(db, subject_key, valid_at, known_at)


def chain(db: ScopedDB, subject_key: str) -> list[StateRow]:
    return bitemporal.chain(db, subject_key)


def heads(db: ScopedDB) -> list[StateRow]:
    return bitemporal.heads(db)


def framework_rollup(db: ScopedDB, framework_id: str) -> FrameworkRollup:
    return knowledge_rollup.rollup(knowledge.load(), heads(db), framework_id)


@dataclass(frozen=True)
class StatusCounts:
    by_status: dict[str, int]
    subjects: int

    @property
    def no_evaluations_yet(self) -> bool:
        return self.subjects == 0


def status_counts(db: ScopedDB) -> StatusCounts:
    rows = heads(db)
    counts: dict[str, int] = {}
    for row in rows:
        counts[row.status.value] = counts.get(row.status.value, 0) + 1
    return StatusCounts(by_status=dict(sorted(counts.items())), subjects=len(rows))


def format_row(row: StateRow) -> str:
    superseded = f" supersedes={row.supersedes_row_id}" if row.supersedes_row_id else ""
    valid_to = f" to {row.valid_to.isoformat()}" if row.valid_to else ""
    return (
        f"{row.status.value:<13} {row.subject_key:<52} valid from {row.valid_from.isoformat()}"
        f"{valid_to}  recorded #{row.recorded_seq} {row.recorded_at.isoformat()}"
        f"  reason={row.reason}{superseded}  verdict={row.verdict_id}"
    )
