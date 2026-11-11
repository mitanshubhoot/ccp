"""Lookups about failing resources, before anyone investigates them (spec.md A-158).

    verdicts --LookupsConsumer--> lookups (a job per failing verdict whose test has a plan)
             --LookupJobs-------> per failing resource: AWS Config history in 7-day windows,
                                  CloudTrail events at under 2 requests a second, and a
                                  search of the organization's Terraform for the resource;
                                  every response landed as raw evidence first, then a typed
                                  row in `lookups`
                              --> the verdict's investigation job

Code chooses every read: the failing resources come from the verdict's own record of them,
and what to read about each from ccp/knowledge/lookups.yaml. The reads use the connector's
read credentials in this process; the Investigator, which holds none, reads the stored rows
through its `get_lookups` tool. A lookup that cannot run (no connection, a refused call) is
recorded as skipped or error, and the investigation is queued all the same.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg

from ccp.config import Settings
from ccp.connectors import rate
from ccp.connectors.github.client import GitHubLiveConnector, search_code
from ccp.connectors.registry import build_connector
from ccp.evaluators import verdict as verdicts
from ccp.evaluators.verdict import Verdict
from ccp.ingest import connections, event_log
from ccp.ingest.consumers import OrderedConsumer, ParallelConsumer, TerminalError
from ccp.ingest.event_log import LogRecord
from ccp.ingest.landing import land_received
from ccp.ingest.raw_zone import open_zone
from ccp.ingest.worker import register
from ccp.knowledge import lookups as plans
from ccp.knowledge.lookups import LookupKind, Plan
from ccp.lookups import store, terraform
from ccp.lookups.store import failing_of
from ccp.storage.scoped import ScopedDB
from ccp.types import VerdictStatus

JOB_TOPIC = "lookups"
INVESTIGATIONS = "investigations"  # the investigation runner's job topic
LOOKED_UP = (VerdictStatus.failed, VerdictStatus.needs_review)
TRAIL_RATE = 2.0  # CloudTrail LookupEvents: 2 requests a second per account and region
MAX_EVENTS = 50  # the newest events kept per resource

Connectors = Callable[[str], Any | None]  # connector name -> a live connector, or None


@dataclass
class Summary:
    consumed: int = 0
    enqueued: int = 0
    skipped: int = 0
    looked_up: int = 0
    landed: int = 0
    investigations: int = 0
    lookup_ids: list[str] = field(default_factory=list)


def job_key(verdict: Verdict) -> str:
    return f"{verdict.tenant_id}:{verdict.verdict_id}"


def resource_id(resource_type: str, failing: dict[str, str]) -> str:
    """The id AWS Config knows a resource by: a bucket's name, an IAM user's immutable id
    (the grant's entity id carries it: aws:grant:<user id>:<account>)."""
    if resource_type == "AWS::IAM::User" and failing["entity_id"].startswith("aws:grant:"):
        return failing["entity_id"].split(":")[2]
    return failing["name"]


def default_connectors(db: ScopedDB, settings: Settings) -> Connectors:
    """The tenant's enabled live connections, built on first use; None when not connected."""
    built: dict[str, Any | None] = {}

    def get(name: str) -> Any | None:
        if name not in built:
            connection = connections.for_pull(db, name, "live")
            built[name] = (
                None if connection is None else build_connector(name, settings, "live", connection)  # type: ignore[arg-type]
            )
        return built[name]

    return get


# -- results, typed ----------------------------------------------------------------------


def _changed(before: dict[str, Any], after: dict[str, Any]) -> list[str]:
    keys = set(before) | set(after)
    return sorted(k for k in keys if before.get(k) != after.get(k))


def _as_dict(value: Any) -> dict[str, Any]:
    """Config sends `configuration` as a JSON string and supplementary values as a map."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return {}
    return dict(value) if isinstance(value, dict) else {}


def history_result(items: list[dict[str, Any]]) -> dict[str, Any]:
    """Config's states, oldest first, each with what changed since the one before."""
    states: list[dict[str, Any]] = []
    previous: dict[str, Any] | None = None
    for item in items:
        shape = {
            **_as_dict(item.get("configuration")),
            **_as_dict(item.get("supplementaryConfiguration")),
        }
        states.append(
            {
                "captured_at": str(item.get("configurationItemCaptureTime")),
                "status": item.get("configurationItemStatus"),
                "state_id": item.get("configurationStateId"),
                "changed": _changed(previous, shape) if previous is not None else [],
            }
        )
        previous = shape
    return {"states": states}


def trail_result(events: list[dict[str, Any]], names: tuple[str, ...]) -> dict[str, Any]:
    """The events that can change the setting, newest first: who, what and when."""
    kept = [e for e in events if e.get("EventName") in names][:MAX_EVENTS]
    return {
        "events": [
            {
                "event_id": e.get("EventId"),
                "event_name": e.get("EventName"),
                "event_time": str(e.get("EventTime")),
                "username": e.get("Username"),
                "event_source": e.get("EventSource"),
            }
            for e in kept
        ],
        "seen": len(events),
    }


def code_result(
    hits: list[dict[str, Any]], term: str, types: tuple[str, ...], by_type: bool = False
) -> dict[str, Any]:
    """The files that name the resource, with the Terraform blocks that do; searched by
    type, every block of the types."""
    files: list[dict[str, Any]] = []
    for hit in hits:
        named = (
            [b for b in terraform.blocks(hit["text"]) if b.type in types]
            if by_type
            else terraform.mentioning(hit["text"], term, types)
        )
        if not named:
            continue
        files.append(
            {
                "repo": hit["repo"],
                "path": hit["path"],
                "sha": hit.get("sha"),
                "resources": [
                    {"address": b.address, "start_line": b.start_line, "end_line": b.end_line}
                    for b in named
                ],
                "text": hit["text"],
            }
        )
    return {"files": files, "searched": len(hits)}


# -- one verdict ---------------------------------------------------------------------------


class _Run:
    def __init__(
        self,
        db: ScopedDB,
        settings: Settings,
        verdict: Verdict,
        connectors: Connectors,
        now: datetime,
    ) -> None:
        self.db = db
        self.settings = settings
        self.verdict = verdict
        self.connectors = connectors
        self.zone = open_zone(settings)
        self.end = now
        self.start = now - timedelta(days=settings.ccp_lookup_days)
        self.ids: list[str] = []
        self.landed = 0

    def _land(self, connector: Any) -> list[str]:
        received = list(getattr(connector, "responses", ()))
        ids = land_received(
            self.db, self.zone, connector.name, received, f"lookup:{self.verdict.verdict_id}"
        )
        self.landed += len(ids)
        return ids

    def _record(
        self,
        failing: dict[str, str],
        kind: LookupKind,
        params: dict[str, Any],
        status: store.Status,
        result: dict[str, Any],
        response_ids: list[str],
        error: str | None = None,
    ) -> None:
        self.ids.append(
            store.record(
                self.db,
                verdict_id=self.verdict.verdict_id,
                test_id=self.verdict.test_id,
                resource=failing["name"],
                entity_id=failing["entity_id"] or None,
                kind=kind,
                params=params,
                status=status,
                result=result,
                response_ids=response_ids,
                error=error,
            )
        )

    def _attempt(
        self,
        failing: dict[str, str],
        kind: LookupKind,
        params: dict[str, Any],
        connector: Any | None,
        read: Callable[[Any], dict[str, Any]],
        count: Callable[[dict[str, Any]], int],
    ) -> None:
        if connector is None:
            self._record(failing, kind, params, "skipped", {}, [], "no live connection")
            return
        try:
            result = read(connector)
        except Exception as exc:
            ids = self._land(connector)  # what came back before the failure is kept too
            self._record(failing, kind, params, "error", {}, ids, f"{type(exc).__name__}: {exc}")
            return
        ids = self._land(connector)
        self._record(failing, kind, params, "ok" if count(result) else "empty", result, ids)

    def resource(self, plan: Plan, failing: dict[str, str]) -> None:
        source = self.connectors(plan.connector)
        if plan.config_history:
            kind = plan.config_history
            rid = resource_id(kind, failing)
            self._attempt(
                failing,
                "config_history",
                {"resource_type": kind, "resource_id": rid, "days": self.settings.ccp_lookup_days},
                source,
                lambda c: history_result(c.config_history(kind, rid, self.start, self.end)),
                lambda r: len(r["states"]),
            )
        if plan.cloudtrail is not None:
            trail = plan.cloudtrail
            key, value = (
                ("ResourceName", failing["name"])
                if trail.by == "resource_name"
                else ("EventSource", trail.source or "")
            )
            account = getattr(source, "account_id", "") if source is not None else ""
            bucket = rate.bucket_for(
                self.db.tenant.tenant_id,
                f"cloudtrail:{account}",
                lambda: rate.RateBucket(TRAIL_RATE, 1),
            )
            self._attempt(
                failing,
                "cloudtrail",
                {"attribute": key, "value": value, "events": list(trail.events)},
                source,
                lambda c: trail_result(
                    c.trail_events(key, value, self.start, self.end, bucket), trail.events
                ),
                lambda r: len(r["events"]),
            )
        if plan.infra_code:
            types = plan.infra_code
            by_type = plan.infra_search == "type"
            term = types[0] if by_type else failing["name"]
            github = self.connectors("github")
            self._attempt(
                failing,
                "infra_code",
                {"term": term, "resource_types": list(types), "by": plan.infra_search},
                github,
                lambda c: code_result(
                    search_code(c, self.db.tenant, term)
                    if isinstance(c, GitHubLiveConnector)
                    else c.search_code(self.db.tenant, term),
                    term,
                    types,
                    by_type,
                ),
                lambda r: len(r["files"]),
            )


def look_up(
    db: ScopedDB,
    settings: Settings,
    verdict: Verdict,
    connectors: Connectors | None = None,
    now: datetime | None = None,
) -> tuple[list[str], int]:
    """Run the verdict's lookups once: the lookup ids written and the responses landed."""
    plan = plans.plan_for(verdict.test_id)
    if plan is None or verdict.status != VerdictStatus.failed:
        return [], 0
    run = _Run(
        db,
        settings,
        verdict,
        connectors or default_connectors(db, settings),
        now or datetime.now(UTC),
    )
    for failing in failing_of(verdict)[: settings.ccp_lookup_max_resources]:
        run.resource(plan, failing)
    return run.ids, run.landed


def queue_investigation(cur: psycopg.Cursor[Any], verdict: Verdict) -> bool:
    """The verdict's investigation job, once (the investigation runner skips verdicts whose
    test has a lookup plan: this is where their job comes from)."""
    key = job_key(verdict)
    queued = cur.execute(
        "SELECT 1 FROM event_log WHERE topic = %s AND partition_key = %s"
        " AND status <> 'dead' LIMIT 1",
        (INVESTIGATIONS, key),
    ).fetchone()
    if queued is not None:
        return False
    event_log.append(
        cur,
        tenant_id=verdict.tenant_id,
        topic=INVESTIGATIONS,
        partition_key=key,
        source_ts=verdict.valid_from,
        ref=verdict.verdict_id,
    )
    return True


class LookupsConsumer(OrderedConsumer):
    """A third consumer of the verdicts topic: queues one lookup job per fail or
    needs_review verdict whose test has a lookup plan. No network call here, so the
    ordered batch is never held open across one."""

    name = "lookups-queue"
    topic = "verdicts"

    def __init__(self, db: ScopedDB, settings: Settings, summary: Summary | None = None) -> None:
        super().__init__(db)
        self.settings = settings
        self.summary = summary if summary is not None else Summary()

    def handle(self, cur: psycopg.Cursor[Any], record: LogRecord) -> None:
        self.summary.consumed += 1
        verdict = verdicts.load(self._db, record.ref)
        if (
            verdict is None
            or verdict.status not in LOOKED_UP
            or plans.plan_for(verdict.test_id) is None
        ):
            self.summary.skipped += 1
            return
        key = job_key(verdict)
        queued = cur.execute(
            "SELECT 1 FROM event_log WHERE topic = %s AND partition_key = %s"
            " AND status <> 'dead' LIMIT 1",
            (JOB_TOPIC, key),
        ).fetchone()
        if queued is not None:
            self.summary.skipped += 1
            return
        event_log.append(
            cur,
            tenant_id=verdict.tenant_id,
            topic=JOB_TOPIC,
            partition_key=key,
            source_ts=verdict.valid_from,
            ref=verdict.verdict_id,
        )
        self.summary.enqueued += 1


class LookupJobs(ParallelConsumer):
    """Runs one verdict's lookups per job, outside any transaction, then queues its
    investigation. A retried job whose lookups are stored only queues the investigation."""

    name = "lookups"
    topic = JOB_TOPIC
    one_per_key = True

    def __init__(
        self,
        db: ScopedDB,
        settings: Settings,
        connectors: Connectors | None = None,
        summary: Summary | None = None,
        now: datetime | None = None,
    ) -> None:
        super().__init__(
            db,
            max_attempts=settings.ccp_max_attempts,
            visibility_seconds=settings.ccp_claim_visibility_seconds,
            base_delay=settings.ccp_retry_base_seconds,
        )
        self.settings = settings
        self.connectors = connectors
        self.summary = summary if summary is not None else Summary()
        self.now = now

    def handle(self, record: LogRecord) -> None:
        verdict = verdicts.load(self._db, record.ref)
        if verdict is None:
            raise TerminalError(f"verdict {record.ref!r} is not stored")
        if not store.for_verdict(self._db, verdict.verdict_id):
            ids, landed = look_up(self._db, self.settings, verdict, self.connectors, self.now)
            self.summary.looked_up += len(ids)
            self.summary.landed += landed
            self.summary.lookup_ids.extend(ids)
        with self._db.transaction() as cur:
            if queue_investigation(cur, verdict):
                self.summary.investigations += 1


register("lookups-queue")(lambda db, settings: LookupsConsumer(db, settings))
register("lookups")(lambda db, settings: LookupJobs(db, settings))


def drain(
    db: ScopedDB,
    settings: Settings,
    connectors: Connectors | None = None,
    now: datetime | None = None,
) -> Summary:
    """Queue the lookups for every new failing verdict with a plan, then run them; each
    ends by queueing its verdict's investigation."""
    summary = Summary()
    queue = LookupsConsumer(db, settings, summary)
    while queue.run_once():
        pass
    jobs = LookupJobs(db, settings, connectors, summary, now)
    while jobs.run_once():
        pass
    return summary
