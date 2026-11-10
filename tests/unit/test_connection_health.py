"""MT1 (A-121): a connection's health, derived from its newest check and its sync runs."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from ccp.ingest.connections import Check, derive

NOW = datetime(2026, 11, 10, 12, tzinfo=UTC)
DAY = timedelta(hours=26)


def _check(outcome: str, hours_ago: float) -> Check:
    return Check(
        "github-live-acme", outcome, f"probe said {outcome}", NOW - timedelta(hours=hours_ago)
    )


def _run(status: str, kind: str | None, hours_ago: float) -> tuple[str, str | None, datetime]:
    return status, kind, NOW - timedelta(hours=hours_ago)


@pytest.mark.parametrize(
    ("case", "check", "run", "complete_hours_ago", "state"),
    [
        ("never synced", None, None, None, "stale"),
        ("checked but never synced", _check("ok", 1), None, None, "stale"),
        ("a complete pull today", _check("ok", 3), _run("complete", None, 2), 2, "healthy"),
        ("no check yet, a complete pull", None, _run("complete", None, 2), 2, "healthy"),
        (
            "the last complete pull is too old",
            _check("ok", 1),
            _run("complete", None, 30),
            30,
            "stale",
        ),
        ("the latest pull was partial", _check("ok", 3), _run("partial", None, 2), 20, "degraded"),
        ("the latest pull failed", _check("ok", 3), _run("failed", "fail", 2), 20, "degraded"),
        ("the newest check failed", _check("fail", 1), _run("complete", None, 2), 2, "degraded"),
        (
            "the newest check was refused",
            _check("auth", 1),
            _run("complete", None, 2),
            2,
            "auth_failed",
        ),
        (
            "a refused pull after an ok check",
            _check("ok", 3),
            _run("failed", "auth", 2),
            20,
            "auth_failed",
        ),
        (
            "an ok check after a refused pull",
            _check("ok", 1),
            _run("failed", "auth", 2),
            20,
            "degraded",
        ),
        (
            "the account went out of sight",
            _check("not_visible", 1),
            _run("complete", None, 5),
            5,
            "not_visible",
        ),
        (
            "a pull that found the account hidden",
            None,
            _run("failed", "not_visible", 1),
            20,
            "not_visible",
        ),
    ],
)
def test_health(
    case: str,
    check: Check | None,
    run: tuple[str, str | None, datetime] | None,
    complete_hours_ago: float | None,
    state: str,
) -> None:
    last_complete = (
        None if complete_hours_ago is None else NOW - timedelta(hours=complete_hours_ago)
    )
    got, reason = derive(
        is_enabled=True,
        last_check=check,
        last_run=run,
        last_complete=last_complete,
        now=NOW,
        stale_after=DAY,
    )
    assert got == state, (case, reason)
    assert reason


def test_a_connection_no_longer_declared_is_disabled_whatever_it_said() -> None:
    got, _ = derive(
        is_enabled=False,
        last_check=_check("auth", 1),
        last_run=_run("complete", None, 1),
        last_complete=NOW,
        now=NOW,
        stale_after=DAY,
    )
    assert got == "disabled"
