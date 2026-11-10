"""The connector conformance harness (spec.md A-122).

Every live connector runs a suite of recorded scenarios through its real code, offline, and
the harness checks what the sync contract promises (A-118 to A-121):

    the run ends as the scenario says    complete, partial, or stopped with a named kind
    a hidden scope is never complete     what the scenario hides, the report must not claim
    absence only from a whole listing    a listing the scenario hides is never settled
    every stop names its kind            a PullError (auth, not_visible or fail), nothing else
    events are well formed               the tenant's, with a provider id and an aware time
    an unchanged re-pull is the same     the same snapshot ids, so nothing lands again
    waits are honoured                   a rate limit's wait happens before the retry
    recordings hold no secret            no token and no Authorization header in a recording

Negative controls prove the harness can fail: each is the real connector with one promise
broken (a hidden listing read as empty, a scope claimed complete over a gap, ids that carry
the pull time). A control the harness passes means it has gone blind, and the run fails.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Protocol

from ccp.config import TenantSettings
from ccp.connectors.base import (
    Connector,
    PullError,
    PullErrorKind,
    RunStatus,
    ScopeStatus,
    SourceEvent,
    SyncReport,
)
from ccp.tenancy import TenantContext, TenantId

Sleep = Callable[[float], None]


@dataclass(frozen=True)
class Expect:
    status: RunStatus | None = None  # how a pull that runs to its end ends
    stops: PullErrorKind | None = None  # or the kind it stops with
    scopes: Mapping[str, ScopeStatus] = field(default_factory=dict)
    waits: tuple[float, ...] = ()  # rate-limit waits that must happen


@dataclass(frozen=True)
class Scenario:
    name: str
    describe: str
    expect: Expect
    hidden: frozenset[str] = frozenset()  # listings the scenario hides from the credential
    repeatable: bool = False  # pulled twice, the snapshots must be the same events
    check: Callable[[list[SourceEvent]], list[str]] | None = None  # scenario-specific facts


class Suite(Protocol):
    connector: str
    scenarios: tuple[Scenario, ...]

    def build(self, scenario: Scenario, sleep: Sleep) -> Connector: ...

    def settled(self, events: list[SourceEvent]) -> set[str] | None:
        """The listings this pull settles (an absence there becomes a revocation), or None
        for a connector that settles nothing."""
        ...

    def recordings(self) -> Iterable[str]: ...

    def close(self) -> None: ...


@dataclass(frozen=True)
class Result:
    suite: str
    scenario: str
    problems: tuple[str, ...]

    @property
    def passed(self) -> bool:
        return not self.problems


@dataclass(frozen=True)
class Control:
    """A negative control: a suite whose connector breaks one promise. It must fail."""

    name: str
    breaks: str
    suite: Suite


@dataclass(frozen=True)
class Report:
    results: tuple[Result, ...]
    controls: tuple[tuple[Control, tuple[Result, ...]], ...]

    @property
    def blind(self) -> list[str]:
        """Controls the harness passed: it could not see the promise each one breaks."""
        return [c.name for c, results in self.controls if all(r.passed for r in results)]

    @property
    def green(self) -> bool:
        return all(r.passed for r in self.results) and not self.blind


SECRET_PATTERNS = (
    re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"),  # GitHub tokens
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),  # an AWS access key id
    re.compile(r"(?i)\bauthorization\b[\"']?\s*[:=]"),  # a recorded header, quoted or not
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-]{8,}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
)


def unsafe(text: str) -> list[str]:
    """What in a recording looks like a credential; a recording must hold none."""
    return [p.pattern for p in SECRET_PATTERNS if p.search(text)]


def harness_tenant() -> TenantContext:
    settings = TenantSettings(
        display_name="Conformance",
        github_org="conformance-example",
        org_admin_limit=1,
        critical_asset_tags=(),
        termination_revocation_sla_hours=24,
    )
    return TenantContext(TenantId("conformance"), settings)


def _pull(
    suite: Suite, scenario: Scenario, tenant: TenantContext
) -> tuple[list[SourceEvent], SyncReport, PullErrorKind | None, list[float], list[str]]:
    sleeps: list[float] = []
    connector = suite.build(scenario, sleeps.append)
    report = SyncReport(connector.scopes)
    events: list[SourceEvent] = []
    problems: list[str] = []
    stopped: PullErrorKind | None = None
    try:
        for event in connector.pull(tenant, None, report):
            events.append(event)
    except PullError as exc:
        stopped, report.error = exc.kind, str(exc)
    except Exception as exc:  # a stop must say why, in the error table's terms
        stopped, report.error = "fail", str(exc)
        problems.append(f"stopped with {type(exc).__name__}, not a PullError naming its kind")
    return events, report, stopped, sleeps, problems


def _snapshot_ids(events: list[SourceEvent]) -> list[str]:
    """Event ids, less each pull's own completion marker, which is new every pull (A-119)."""
    return [e.source_event_id for e in events if not e.event_type.endswith("pull.completed")]


def run_scenario(suite: Suite, scenario: Scenario, tenant: TenantContext) -> Result:
    events, report, stopped, sleeps, problems = _pull(suite, scenario, tenant)
    want = scenario.expect
    if stopped != want.stops:
        expected = f"to stop as {want.stops}" if want.stops else "to run to its end"
        said = f"stopped as {stopped}" if stopped else "ran to its end"
        problems.append(f"expected {expected}; it {said}")
    if stopped is None and want.status is not None and report.status() != want.status:
        problems.append(f"expected a {want.status} run; it was {report.status()}")
    outcomes = report.outcomes()
    for scope, status in want.scopes.items():
        got = outcomes[scope].status if scope in outcomes else "undeclared"
        if got != status:
            problems.append(f"scope {scope}: expected {status}, reported {got}")
    settled = suite.settled(events)
    leaked = sorted(settled & scenario.hidden) if settled is not None else []
    if leaked:
        problems.append(f"settled listings it did not see whole: {leaked}")
    for event in events:
        if event.tenant_id != tenant.tenant_id:
            problems.append(f"event {event.source_event_id} names tenant {event.tenant_id!r}")
        if not event.source_event_id or event.source_ts.tzinfo is None:
            problems.append(f"event {event.event_type} lacks a provider id or an aware time")
    for wait in want.waits:
        if not any(abs(s - wait) < 0.01 for s in sleeps):
            problems.append(f"a rate limit asked for {wait}s; the waits were {sleeps}")
    if scenario.check is not None:
        problems.extend(scenario.check(events))
    if scenario.repeatable and stopped is None:
        again, _, _, _, _ = _pull(suite, scenario, tenant)
        if _snapshot_ids(again) != _snapshot_ids(events):
            problems.append("an unchanged re-pull produced different snapshot ids")
    return Result(suite.connector, scenario.name, tuple(problems))


def run_suite(suite: Suite, tenant: TenantContext | None = None) -> tuple[Result, ...]:
    tenant = tenant or harness_tenant()
    try:
        results = [run_scenario(suite, s, tenant) for s in suite.scenarios]
        found = [p for text in suite.recordings() for p in unsafe(text)]
        results.append(Result(suite.connector, "recordings hold no secret", tuple(found)))
        return tuple(results)
    finally:
        suite.close()


def run(suites: Iterable[Suite], controls: Iterable[Control]) -> Report:
    """Every suite, then every negative control. The scenarios provoke the connectors' own
    warnings on purpose, so connector logging is quieted while they run, then restored."""
    log = logging.getLogger("ccp.connectors")
    level = log.level
    log.setLevel(logging.ERROR)
    try:
        results = tuple(r for suite in suites for r in run_suite(suite))
        checked = tuple((control, run_suite(control.suite)) for control in controls)
    finally:
        log.setLevel(level)
    return Report(results, checked)


def default() -> tuple[list[Suite], list[Control]]:
    """Every live connector's suite and its negative controls."""
    from ccp.connectors.github import conformance as github
    from ccp.connectors.manual import conformance as uploads

    return [github.Suite(), uploads.Suite()], [*github.controls(), *uploads.controls()]
