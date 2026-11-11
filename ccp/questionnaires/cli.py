"""`ccp questionnaires ...`: draft answers by the fixed pipeline, review them, and record
that a person sent them (spec.md A-163)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import typer
import yaml

from ccp.config import Settings
from ccp.evaluators.tier2 import Providers
from ccp.observability import steps
from ccp.questionnaires import pipeline, send, store
from ccp.storage import db
from ccp.storage.scoped import ScopedDB
from ccp.tenancy import TenantContext

app = typer.Typer(no_args_is_help=True, help="Security questionnaires: drafted, approved, sent")


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


@app.command("create")
def create(
    file: Path = typer.Option(..., "--file", help="YAML: title, requester, questions[id, text]"),
    tenant: str | None = typer.Option(None, "--tenant"),
) -> None:
    raw = yaml.safe_load(file.read_text(encoding="utf-8")) or {}
    settings = _settings()
    scoped, pool = _scoped(settings, tenant)
    try:
        typer.echo(
            store.create(
                scoped,
                str(raw.get("title") or file.stem),
                list(raw.get("questions") or []),
                str(raw.get("requester") or ""),
            )
        )
    finally:
        pool.close()


@app.command("answer")
def answer(
    questionnaire: str = typer.Option(..., "--questionnaire"),
    tenant: str | None = typer.Option(None, "--tenant"),
) -> None:
    """Draft every question without a live answer."""
    settings = _settings()
    scoped, pool = _scoped(settings, tenant)
    try:
        drafted = pipeline.answer_all(
            scoped, settings, Providers.from_settings(settings), questionnaire
        )
        typer.echo(f"drafted {len(drafted)}")
    finally:
        pool.close()


@app.command("show")
def show(
    questionnaire: str = typer.Option(..., "--questionnaire"),
    tenant: str | None = typer.Option(None, "--tenant"),
) -> None:
    settings = _settings()
    scoped, pool = _scoped(settings, tenant)
    try:
        for a in store.answers_of(scoped, questionnaire):
            failed = [c["name"] for c in a.checks if not c.get("passed")]
            typer.echo(
                f"{a.question_id:<12} {a.status:<9} {a.answer:<22} {a.answer_id}"
                + (f"  failed: {failed}" if failed else "")
            )
            typer.echo(f"    {a.final_text}")
    finally:
        pool.close()


@app.command("approve")
def approve(
    answer_id: str = typer.Option(..., "--answer"),
    by: str = typer.Option(..., "--by"),
    text: str | None = typer.Option(None, "--text", help="your edit of the drafted text"),
    tenant: str | None = typer.Option(None, "--tenant"),
) -> None:
    settings = _settings()
    scoped, pool = _scoped(settings, tenant)
    try:
        store.approve(scoped, answer_id, by, text)
        typer.echo(f"{answer_id}: approved by {by}")
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc
    finally:
        pool.close()


@app.command("reject")
def reject(
    answer_id: str = typer.Option(..., "--answer"),
    by: str = typer.Option(..., "--by"),
    note: str = typer.Option(..., "--note"),
    tenant: str | None = typer.Option(None, "--tenant"),
) -> None:
    settings = _settings()
    scoped, pool = _scoped(settings, tenant)
    try:
        store.reject(scoped, answer_id, by, note)
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc
    finally:
        pool.close()


@app.command("stale")
def stale(tenant: str | None = typer.Option(None, "--tenant")) -> None:
    settings = _settings()
    scoped, pool = _scoped(settings, tenant)
    try:
        typer.echo(f"marked stale {send.sweep(scoped)}")
    finally:
        pool.close()


@app.command("send")
def send_cmd(
    questionnaire: str = typer.Option(..., "--questionnaire"),
    by: str = typer.Option(..., "--by"),
    tenant: str | None = typer.Option(None, "--tenant"),
) -> None:
    """Record that you sent it; every answer must be approved and current."""
    settings = _settings()
    scoped, pool = _scoped(settings, tenant)
    try:
        typer.echo(str(send.send(scoped, settings, questionnaire, by)))
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc
    finally:
        pool.close()


@app.command("metrics")
def metrics(tenant: str | None = typer.Option(None, "--tenant")) -> None:
    settings = _settings()
    scoped, pool = _scoped(settings, tenant)
    try:
        typer.echo(json.dumps(send.metrics(scoped).__dict__, indent=2, default=str))
    finally:
        pool.close()
