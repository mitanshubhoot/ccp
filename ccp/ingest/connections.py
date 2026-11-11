"""Connections as data, checked daily (spec.md A-121).

A connection is one connector reading one provider account, from a fixture or live, with the
purpose name of the credential it reads (never the secret). A tenant declares its
connections in config/tenants.yaml (three fixture connections by default); `sync` brings the
table in line, appending a new version for a new or changed connection and a disabled
version for one no longer declared, so the history says which account was connected when.

    connections         versioned and append-only; current_connections is the newest version
    connection_checks   append-only: the daily probe of each enabled connection's credential
    sync_runs           every pull, carrying the connection it pulled (A-118)

Health is derived, never stored, from the newest check and the runs:

    disabled      no longer declared
    auth_failed   the newest word on the credential, a check or a failed run, is a refusal
    not_visible   the newest word on the credential is that the account is hidden from it
    stale         no complete pull within ccp_sync_stale_hours, or none ever
    degraded      the latest pull was partial or failed, or the newest check failed
    healthy       otherwise
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

import psycopg
from ulid import ULID

from ccp.config import Settings
from ccp.connectors.base import Connection, Connector, ConnectorName, PullError, SourceKind
from ccp.connectors.registry import build_connector
from ccp.ingest import aws_accounts
from ccp.storage.scoped import ScopedDB
from ccp.tenancy import TenantContext

CheckOutcome = Literal["ok", "auth", "not_visible", "fail"]
HealthState = Literal["disabled", "auth_failed", "not_visible", "stale", "degraded", "healthy"]
Build = Callable[[ConnectorName, Settings, SourceKind, Connection | None], Connector]


@dataclass(frozen=True)
class Check:
    connection_id: str
    outcome: CheckOutcome
    detail: str
    checked_at: datetime


@dataclass(frozen=True)
class Health:
    connection: Connection
    state: HealthState
    reason: str
    last_check: Check | None
    last_complete: datetime | None


def connection_id(connector: str, source: str, account: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", f"{connector}-{source}-{account}".lower()).strip("-")


def declared(tenant: TenantContext) -> list[Connection]:
    """The tenant's configured connections, each account resolved: a GitHub connection reads
    the tenant's organization unless it names another, any other the tenant's own account."""
    out: list[Connection] = []
    for spec in tenant.settings.connections:
        default = tenant.settings.github_org if spec.connector == "github" else tenant.tenant_id
        account = spec.account or default
        out.append(
            Connection(
                connection_id=connection_id(spec.connector, spec.source, account),
                version=0,
                connector=spec.connector,
                source=spec.source,
                account=account,
                credential=spec.credential,
                enabled=True,
            )
        )
    return out


def _row(r: Any) -> Connection:
    return Connection(
        connection_id=r[0],
        version=r[1],
        connector=r[2],
        source=r[3],
        account=r[4],
        credential=r[5],
        enabled=r[6],
    )


_COLUMNS = "connection_id, version, connector, source, account, credential, enabled"


def current(db: ScopedDB) -> list[Connection]:
    rows = db.fetch_all(f"SELECT {_COLUMNS} FROM current_connections ORDER BY connection_id")
    return [_with_params(db, _row(r)) for r in rows]


def _with_params(db: ScopedDB, connection: Connection) -> Connection:
    """An AWS connection carries its account's verified role and external ID (A-139); one
    not verified carries none, and its connector refuses to pull."""
    if connection.connector != "aws":
        return connection
    found = aws_accounts.current(db, connection.account)
    if found is None or found.status != "verified" or not found.role_arn:
        return connection
    params = {"role_arn": found.role_arn, "external_id": found.external_id}
    return replace(connection, params=params)


def enabled(db: ScopedDB) -> list[Connection]:
    return [c for c in current(db) if c.enabled]


def for_pull(db: ScopedDB, connector: str, source: str) -> Connection | None:
    """The enabled connection a pull of this connector and source belongs to, if declared."""
    return next((c for c in enabled(db) if (c.connector, c.source) == (connector, source)), None)


def _append(
    cur: psycopg.Cursor[Any], tenant_id: str, c: Connection, version: int, on: bool
) -> None:
    cur.execute(
        f"INSERT INTO connections (tenant_id, {_COLUMNS}, recorded_by)"
        " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING",
        (
            tenant_id,
            c.connection_id,
            version,
            c.connector,
            c.source,
            c.account,
            c.credential,
            on,
            "config/tenants.yaml",
        ),
    )


def sync(db: ScopedDB) -> int:
    """Bring the table in line with the tenant's configuration. Returns the versions
    appended: a new or changed connection, or one no longer declared (disabled, never
    deleted). Two processes syncing at once write the same version once."""
    wanted = {c.connection_id: c for c in declared(db.tenant)}
    appended = 0
    with db.transaction() as cur:
        rows = cur.execute(f"SELECT {_COLUMNS} FROM current_connections").fetchall()
        have = {r[0]: _row(r) for r in rows}
        for cid, want in wanted.items():
            old = have.get(cid)
            if old is not None and old.enabled and old.credential == want.credential:
                continue
            _append(cur, db.tenant.tenant_id, want, (old.version + 1) if old else 1, True)
            appended += 1
        for cid, old in have.items():
            if cid not in wanted and old.enabled:
                _append(cur, db.tenant.tenant_id, old, old.version + 1, False)
                appended += 1
    return appended


def record_check(db: ScopedDB, check: Check) -> None:
    with db.transaction() as cur:
        cur.execute(
            "INSERT INTO connection_checks"
            " (tenant_id, check_id, connection_id, checked_at, outcome, detail)"
            " VALUES (%s, %s, %s, %s, %s, %s)",
            (
                db.tenant.tenant_id,
                str(ULID()),
                check.connection_id,
                check.checked_at,
                check.outcome,
                check.detail[:2000] or None,
            ),
        )


def check(
    settings: Settings, db: ScopedDB, connection: Connection, build: Build = build_connector
) -> Check:
    """Probe one connection's credential and record what it said. A connector with nothing to
    probe (a fixture, the upload folder) is ok once it can be built; one that cannot be
    built, for want of its credential, is a refusal."""
    try:
        connector = build(connection.connector, settings, connection.source, connection)
    except Exception as exc:  # no credential configured for a live source
        outcome: CheckOutcome = "auth"
        detail = f"cannot build the connector: {type(exc).__name__}: {exc}"
    else:
        probe = getattr(connector, "check", None)
        if probe is None:
            outcome, detail = "ok", "nothing to probe"
        else:
            try:
                outcome, detail = probe(db.tenant)
            except PullError as exc:
                outcome, detail = exc.kind, f"{type(exc).__name__}: {exc}"
            except Exception as exc:  # a probe that broke is a failed check, not a crash
                outcome, detail = "fail", f"{type(exc).__name__}: {exc}"
    result = Check(connection.connection_id, outcome, detail, datetime.now(UTC))
    record_check(db, result)
    return result


def check_all(settings: Settings, db: ScopedDB, build: Build = build_connector) -> list[Check]:
    """The daily job: every enabled connection probed, each check recorded."""
    sync(db)
    return [check(settings, db, c, build) for c in enabled(db)]


def derive(
    *,
    is_enabled: bool,
    last_check: Check | None,
    last_run: tuple[str, str | None, datetime] | None,
    last_complete: datetime | None,
    now: datetime,
    stale_after: timedelta,
) -> tuple[HealthState, str]:
    """Health from the newest check, the latest run (status, error kind, finished at) and
    the last complete run. Pure, so every rule has a test."""
    if not is_enabled:
        return "disabled", "no longer declared in the tenant's configuration"
    words: list[tuple[datetime, str, str]] = []  # the newest word on the credential wins
    if last_check is not None:
        words.append((last_check.checked_at, last_check.outcome, f"check: {last_check.detail}"))
    if last_run is not None and last_run[0] == "failed" and last_run[1] is not None:
        words.append((last_run[2], last_run[1], "the latest pull failed"))
    if words:
        _, kind, said = max(words, key=lambda w: w[0])
        if kind == "auth":
            return "auth_failed", f"the credential was refused ({said})"
        if kind == "not_visible":
            return "not_visible", f"the account is hidden from the credential ({said})"
    if last_complete is None:
        return "stale", "no complete pull yet"
    if now - last_complete > stale_after:
        return "stale", f"no complete pull since {last_complete.isoformat()}"
    if last_run is not None and last_run[0] != "complete":
        return "degraded", f"the latest pull was {last_run[0]}"
    if last_check is not None and last_check.outcome != "ok":
        return "degraded", f"the latest check failed: {last_check.detail}"
    return "healthy", f"complete pull at {last_complete.isoformat()}"


def health(
    db: ScopedDB, settings: Settings, connection: Connection, now: datetime | None = None
) -> Health:
    cid = connection.connection_id
    check_row = db.fetch_one(
        "SELECT outcome, detail, checked_at FROM connection_checks WHERE connection_id = %s"
        " ORDER BY checked_at DESC, check_id DESC LIMIT 1",
        (cid,),
    )
    last_check = Check(cid, check_row[0], check_row[1] or "", check_row[2]) if check_row else None
    run_row = db.fetch_one(
        "SELECT status, error_kind, finished_at FROM sync_runs WHERE connection_id = %s"
        " ORDER BY finished_at DESC, run_id DESC LIMIT 1",
        (cid,),
    )
    complete_row = db.fetch_one(
        "SELECT max(finished_at) FROM sync_runs WHERE connection_id = %s AND status = 'complete'",
        (cid,),
    )
    last_complete = complete_row[0] if complete_row else None
    state, reason = derive(
        is_enabled=connection.enabled,
        last_check=last_check,
        last_run=(run_row[0], run_row[1], run_row[2]) if run_row else None,
        last_complete=last_complete,
        now=now or datetime.now(UTC),
        stale_after=timedelta(hours=settings.ccp_sync_stale_hours),
    )
    return Health(connection, state, reason, last_check, last_complete)
