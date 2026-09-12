"""The tenant daily budget (spec.md A-11): spend is recorded in an append-only ledger; a
tenant over its daily cap has its evaluation records parked, never evaluated, and they
resume when the day rolls over (`requeue_parked`)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from ulid import ULID

from ccp.ingest.consumers import ParkedError
from ccp.storage.scoped import ScopedDB


def record_spend(
    db: ScopedDB,
    usd: float,
    tokens: int = 0,
    step_id: str | None = None,
    recorded_at: datetime | None = None,
) -> None:
    if usd <= 0 and tokens <= 0:
        return
    db.execute(
        "INSERT INTO spend_ledger (entry_id, tenant_id, step_id, usd, tokens, recorded_at)"
        " VALUES (%s, %s, %s, %s, %s, coalesce(%s, now()))",
        (str(ULID()), db.tenant.tenant_id, step_id, max(usd, 0.0), tokens, recorded_at),
    )


def _day_window(now: datetime | None) -> tuple[datetime, datetime]:
    moment = (now or datetime.now(UTC)).astimezone(UTC)
    start = moment.replace(hour=0, minute=0, second=0, microsecond=0)
    return start, start + timedelta(days=1)


def daily_spend(db: ScopedDB, now: datetime | None = None) -> float:
    start, end = _day_window(now)
    row = db.fetch_one(
        "SELECT coalesce(sum(usd), 0) FROM spend_ledger"
        " WHERE recorded_at >= %s AND recorded_at < %s",
        (start, end),
    )
    return float(row[0]) if row else 0.0


def check_daily_budget(db: ScopedDB, now: datetime | None = None) -> float:
    """Return today's spend, or raise ParkedError when the tenant is over its daily cap."""
    spent = daily_spend(db, now)
    cap = db.tenant.settings.daily_budget_usd
    if spent >= cap:
        raise ParkedError(
            f"budget_exceeded: tenant daily budget exhausted ({spent:.4f} >= {cap:.2f} USD)"
        )
    return spent
