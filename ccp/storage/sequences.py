"""Per-tenant counters (spec.md A-107, A-125).

A counter is advanced under its row's lock, which is held until the transaction commits, so
a lower number never commits after a higher one. That is what makes a watermark exact: the
rows stamped at or below the committed value are all visible to a later statement, and no
row stamped later can be at or below it.

    observations   every entity observation and identity link or review row (A-125)
"""

from __future__ import annotations

from typing import Any

from ccp.storage.scoped import ScopedDB

OBSERVATIONS = "observations"


def next_value(cur: Any, tenant_id: str, name: str) -> int:
    """Advance the tenant's counter inside the caller's transaction and return the value."""
    row = cur.execute(
        "INSERT INTO tenant_sequences (tenant_id, name, value) VALUES (%s, %s, 1)"
        " ON CONFLICT (tenant_id, name) DO UPDATE SET value = tenant_sequences.value + 1"
        " RETURNING value",
        (tenant_id, name),
    ).fetchone()
    assert row is not None
    return int(row[0])


def current_value(db: ScopedDB, name: str) -> int:
    """The counter as committed now: a watermark for reads (0 before the first stamp)."""
    row = db.fetch_one("SELECT value FROM tenant_sequences WHERE name = %s", (name,))
    return int(row[0]) if row else 0
