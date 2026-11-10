"""A-108: a schedule's next slot, on a tenant's wall clock and across daylight saving."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from ccp.ingest.schedules import next_slot

NY = "America/New_York"


def test_daily_runs_at_the_local_time() -> None:
    after = datetime(2026, 1, 10, 12, tzinfo=UTC)
    assert next_slot("daily 02:00", NY, after) == datetime(2026, 1, 11, 7, tzinfo=UTC)  # EST
    assert next_slot("daily 02:00", "Europe/Berlin", after) == datetime(2026, 1, 11, 1, tzinfo=UTC)


def test_a_time_in_the_spring_gap_runs_after_the_gap_once() -> None:
    # 2026-03-08: New York skips 02:00-03:00
    first = next_slot("daily 02:30", NY, datetime(2026, 3, 8, 5, tzinfo=UTC))
    assert first == datetime(2026, 3, 8, 7, 30, tzinfo=UTC)  # 03:30 EDT
    assert next_slot("daily 02:30", NY, first) == datetime(2026, 3, 9, 6, 30, tzinfo=UTC)


def test_a_repeated_time_on_the_fall_back_night_runs_once() -> None:
    # 2026-11-01: New York runs 01:00-02:00 twice
    first = next_slot("daily 01:30", NY, datetime(2026, 11, 1, 4, tzinfo=UTC))
    assert first == datetime(2026, 11, 1, 5, 30, tzinfo=UTC)  # the first 01:30, EDT
    after_second = datetime(2026, 11, 1, 6, 45, tzinfo=UTC)  # past the second 01:30, EST
    assert next_slot("daily 01:30", NY, first) == datetime(2026, 11, 2, 6, 30, tzinfo=UTC)
    assert next_slot("daily 01:30", NY, after_second) == datetime(2026, 11, 2, 6, 30, tzinfo=UTC)


def test_every_aligns_to_the_clock() -> None:
    after = datetime(2026, 1, 1, 10, 7, 30, tzinfo=UTC)
    assert next_slot("every 15 minutes", NY, after) == datetime(2026, 1, 1, 10, 15, tzinfo=UTC)
    on_the_dot = datetime(2026, 1, 1, 10, 15, tzinfo=UTC)
    assert next_slot("every 15 minutes", NY, on_the_dot) == datetime(2026, 1, 1, 10, 30, tzinfo=UTC)
    assert next_slot("every 2 hours", NY, after) == datetime(2026, 1, 1, 12, tzinfo=UTC)


def test_an_unknown_form_is_refused() -> None:
    with pytest.raises(ValueError):
        next_slot("weekly mon 02:00", NY, datetime(2026, 1, 1, tzinfo=UTC))
