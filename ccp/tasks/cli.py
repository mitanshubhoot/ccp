"""``ccp tasks ...``: long tasks as typed rows (spec.md A-150)."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

import typer
import yaml

from ccp.config import Settings
from ccp.observability import steps
from ccp.storage import db
from ccp.storage.scoped import ScopedDB
from ccp.tasks import runner, store
from ccp.tenancy import TenantContext

app = typer.Typer(no_args_is_help=True, help="Long tasks: planned by code, run item by item")


def _settings() -> Settings:
    settings = Settings()
    steps.configure(settings)
    return settings


def _scoped(settings: Settings, tenant_id: str | None) -> tuple[ScopedDB, Any]:
    chosen = tenant_id or settings.ccp_tenant
    if not chosen:
        raise typer.BadParameter("pass --tenant or set CCP_TENANT")
    pool = db.app_pool(settings)
    return ScopedDB(pool, TenantContext.for_id(settings, chosen)), pool


def _when(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def _show(task: store.Task, items: list[store.Item]) -> str:
    lines = [
        f"{task.task_id}  {task.kind}  {task.status}"
        + (f" ({task.parked_reason})" if task.parked_reason else "")
        + f"  {task.title}",
        f"  spent ${task.spent_usd:.4f} of ${task.max_usd:.4f} (forecast ${task.forecast_usd:.4f})"
        f"  deadline {task.deadline.isoformat()}  passes {task.passes}"
        f"  stalled {task.stalled_passes}",
    ]
    for it in items:
        lines.append(
            f"  {it.item_id:<28} {it.kind:<5} {it.status:<12} {it.outcome or '-':<10}"
            f" runs {it.attempts}" + (f"  asks: {it.open_question}" if it.open_question else "")
        )
    return "\n".join(lines)


@app.command("plan")
def plan(
    kind: str = typer.Option(..., "--kind", help="audit_prep | questionnaire"),
    title: str = typer.Option(..., "--title"),
    framework: str | None = typer.Option(None, "--framework", help="audit_prep: soc2, iso27001"),
    family: list[str] = typer.Option([], "--family", help="audit_prep: limit to a family"),
    period_end: str | None = typer.Option(None, "--period-end", help="audit_prep: ISO date"),
    questions: Path | None = typer.Option(None, "--questions", help="questionnaire: YAML"),
    deadline: str | None = typer.Option(None, "--deadline", help="ISO time; default 14 days"),
    max_usd: float | None = typer.Option(None, "--max-usd"),
    by: str = typer.Option("cli", "--by", help="who asked for the task"),
    tenant: str | None = typer.Option(None, "--tenant"),
) -> None:
    """Write a task's items from scoping facts; no model runs here."""
    scope: dict[str, Any]
    if kind == "audit_prep":
        if not framework:
            raise typer.BadParameter("audit_prep needs --framework")
        scope = {"framework_id": framework, "families": family, "period_end": period_end}
    elif kind == "questionnaire":
        if questions is None:
            raise typer.BadParameter("questionnaire needs --questions FILE")
        loaded = yaml.safe_load(questions.read_text(encoding="utf-8")) or {}
        scope = {"questions": loaded.get("questions") or []}
    else:
        raise typer.BadParameter(f"unknown kind {kind!r}")
    settings = _settings()
    scoped, pool = _scoped(settings, tenant)
    try:
        task = runner.plan(
            scoped,
            settings,
            kind,
            title,
            scope,
            created_by=by,
            deadline=_when(deadline),
            max_usd=max_usd,
        )
        typer.echo(_show(task, store.items_of(scoped, task.task_id)))
    finally:
        pool.close()


@app.command("advance")
def advance(
    task: str = typer.Option(..., "--task"),
    tenant: str | None = typer.Option(None, "--tenant"),
) -> None:
    """One pass: caps, checks settled by code, follow-ups added, reads launched."""
    settings = _settings()
    scoped, pool = _scoped(settings, tenant)
    try:
        summary = runner.advance(scoped, settings, task, reader=_reader())
    finally:
        pool.close()
    typer.echo(
        f"{summary.task_id}: {summary.status}; checked {summary.checked}, changed"
        f" {summary.changed}, added {len(summary.added)}, reads waiting {summary.reads_waiting},"
        f" ran {summary.ran}, ${summary.usd:.4f}"
        + (f"; parked: {summary.parked_reason}" if summary.parked_reason else "")
    )
    for event in summary.events:
        typer.echo(f"  {event}")


def _reader() -> runner.Reader | None:
    return None  # the fan-out launcher (A-151) supplies one


@app.command("show")
def show(
    task: str = typer.Option(..., "--task"),
    tenant: str | None = typer.Option(None, "--tenant"),
    history: bool = typer.Option(False, "--history", help="also print every task event"),
) -> None:
    settings = _settings()
    scoped, pool = _scoped(settings, tenant)
    try:
        found = store.get(scoped, task)
        if found is None:
            raise typer.BadParameter(f"no task {task!r}")
        typer.echo(_show(found, store.items_of(scoped, task)))
        if history:
            for e in store.events(scoped, task):
                typer.echo(
                    f"  {e['recorded_at'].isoformat()} {e['kind']:<12} {e['item_id'] or '-'}"
                    f" {json.dumps(e['detail'], default=str)}"
                )
    finally:
        pool.close()


@app.command("list")
def list_(
    status: str | None = typer.Option(None, "--status"),
    tenant: str | None = typer.Option(None, "--tenant"),
) -> None:
    settings = _settings()
    scoped, pool = _scoped(settings, tenant)
    try:
        for t in store.list_tasks(scoped, status):
            typer.echo(
                f"{t.task_id}  {t.kind:<13} {t.status:<9} ${t.spent_usd:.4f}/${t.max_usd:.4f}"
                f"  {t.title}"
            )
    finally:
        pool.close()


@app.command("gaps")
def gaps(
    task: str = typer.Option(..., "--task"),
    tenant: str | None = typer.Option(None, "--tenant"),
) -> None:
    """The gap list: a query over the item rows."""
    settings = _settings()
    scoped, pool = _scoped(settings, tenant)
    try:
        for row in runner.gaps(scoped, task):
            typer.echo(json.dumps(row, default=str, sort_keys=True))
    finally:
        pool.close()


@app.command("resolve")
def resolve(
    task: str = typer.Option(..., "--task"),
    item: str = typer.Option(..., "--item"),
    outcome: str = typer.Option(..., "--outcome"),
    note: str = typer.Option(..., "--note"),
    by: str = typer.Option(..., "--by"),
    tenant: str | None = typer.Option(None, "--tenant"),
) -> None:
    """A person settles an item the runner held for review."""
    settings = _settings()
    scoped, pool = _scoped(settings, tenant)
    try:
        after = runner.resolve(scoped, task, item, outcome=outcome, note=note, by=by)
    finally:
        pool.close()
    typer.echo(f"{after.item_id}: {after.status} ({after.outcome})")


@app.command("cancel")
def cancel(
    task: str = typer.Option(..., "--task"),
    by: str = typer.Option(..., "--by"),
    reason: str = typer.Option(..., "--reason"),
    tenant: str | None = typer.Option(None, "--tenant"),
) -> None:
    settings = _settings()
    scoped, pool = _scoped(settings, tenant)
    try:
        runner.cancel(scoped, task, by=by, reason=reason)
    finally:
        pool.close()
    typer.echo(f"{task}: cancelled")


@app.command("resume")
def resume(
    task: str = typer.Option(..., "--task"),
    by: str = typer.Option(..., "--by"),
    deadline: str | None = typer.Option(None, "--deadline"),
    max_usd: float | None = typer.Option(None, "--max-usd"),
    tenant: str | None = typer.Option(None, "--tenant"),
) -> None:
    """Reopen a parked task, optionally with a new deadline or dollar cap."""
    settings = _settings()
    scoped, pool = _scoped(settings, tenant)
    try:
        runner.resume(scoped, task, by=by, deadline=_when(deadline), max_usd=max_usd)
    finally:
        pool.close()
    typer.echo(f"{task}: open")
