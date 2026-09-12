"""Property test: no sequence of records ever leaves a subject with two open rows, and
every as-of query returns a row that was known at the time and covers the instant asked."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from psycopg_pool import ConnectionPool

from ccp.config import Settings
from ccp.storage import bitemporal
from ccp.storage.scoped import ScopedDB
from ccp.tenancy import TenantContext

pytestmark = pytest.mark.db
BASE = datetime(2026, 1, 1, tzinfo=UTC)

record_strategy = st.lists(
    st.tuples(
        st.sampled_from(["pass", "fail", "partial", "needs_review"]),
        st.integers(min_value=0, max_value=60),  # valid_from day offset
        st.one_of(st.none(), st.integers(min_value=1, max_value=30)),  # valid_to days after from
        st.sampled_from(["evaluation", "correction", "late_event", "human_override"]),
    ),
    min_size=1,
    max_size=8,
)


@settings(
    max_examples=12, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture]
)
@given(records=record_strategy, probe_day=st.integers(min_value=0, max_value=100))
def test_chain_invariants(
    db_settings: Settings,
    pool: ConnectionPool,
    records: list[tuple[str, int, int | None, str]],
    probe_day: int,
) -> None:
    db = ScopedDB(pool, TenantContext.for_id(db_settings, "acme"))
    subject = f"PROP|{datetime.now(UTC).timestamp()}|{abs(hash(tuple(records)))}"
    db.execute(
        "INSERT INTO verdicts (verdict_id, evaluation_key, tenant_id, test_id, subject_key, status,"
        " reasoning, confidence, confidence_cap, tier, trigger_observed_at, valid_from)"
        " VALUES (%s, %s, 'acme', 'PROP', %s, 'pass', 'stub', 1, 1, 1, now(), now())"
        " ON CONFLICT DO NOTHING",
        (f"v-{subject}", f"k-{subject}", subject),
    )
    inserted = []
    for status, from_day, to_days, reason in records:
        valid_from = BASE + timedelta(days=from_day)
        valid_to = valid_from + timedelta(days=to_days) if to_days else None
        row = bitemporal.record(
            db,
            subject_key=subject,
            test_id="PROP",
            control_ids=(),
            status=status,
            verdict_id=f"v-{subject}",
            valid_from=valid_from,
            valid_to=valid_to,
            reason=reason,  # type: ignore[arg-type]
        )
        inserted.append(row)

    chain = bitemporal.chain(db, subject)
    assert [r.row_id for r in chain] == [r.row_id for r in inserted]
    assert [r.supersedes_row_id for r in chain[1:]] == [r.row_id for r in chain[:-1]]
    assert len([h for h in bitemporal.heads(db) if h.subject_key == subject]) == 1
    seqs = [r.recorded_seq for r in chain]
    assert seqs == sorted(seqs)

    probe = BASE + timedelta(days=probe_day)
    now = datetime.now(UTC)
    answer = bitemporal.state_as_of(db, subject, probe, now)
    if answer is not None:
        assert answer.recorded_at <= now
        assert answer.valid_from <= probe
        if answer.valid_to is not None:
            assert probe < answer.valid_to
    # what we believe about an instant before anything was recorded is nothing
    assert bitemporal.state_as_of(db, subject, probe, BASE - timedelta(days=1)) is None
