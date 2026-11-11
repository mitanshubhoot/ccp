"""`ccp fixes ...`: the fixes CCP suggests, what people said about them, and whether a later
read has seen them in (spec.md A-159, A-160). CCP applies none of them."""

from __future__ import annotations

import json
from typing import Any

import typer

from ccp.config import Settings
from ccp.observability import steps
from ccp.remediation import confirm, suggestions
from ccp.storage import db
from ccp.storage.scoped import ScopedDB
from ccp.tenancy import TenantContext

app = typer.Typer(no_args_is_help=True, help="Suggested fixes: a person applies them")


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


def render(found: suggestions.Suggestion) -> str:
    body = found.body
    lines = [
        f"{found.suggestion_id}  {found.status}  {found.test_id}  {found.resource}",
        f"  {body.get('title')} ({found.fix} v{found.fix_version}, from {found.source})",
    ]
    change = body.get("terraform")
    if change:
        lines.append(
            f"  Terraform: {change.get('repo')}/{change.get('path')} ({change.get('address')})"
        )
        lines.extend(f"    {line}" for line in str(change.get("diff", "")).splitlines())
    if body.get("cli"):
        lines.append("  Commands:")
        lines.extend(f"    {c}" for c in body["cli"])
    if body.get("console"):
        lines.append("  Console:")
        lines.extend(f"    {i}. {c}" for i, c in enumerate(body["console"], 1))
    lines.append(f"  Done when: {body.get('verify')}")
    return "\n".join(lines)


@app.command("list")
def list_(
    status: str | None = typer.Option(None, "--status"),
    test: str | None = typer.Option(None, "--test"),
    tenant: str | None = typer.Option(None, "--tenant"),
) -> None:
    settings = _settings()
    scoped, pool = _scoped(settings, tenant)
    try:
        for s in suggestions.list_all(scoped, status, test):
            typer.echo(f"{s.suggestion_id}  {s.status:<10} {s.test_id:<6} {s.resource}  {s.fix}")
    finally:
        pool.close()


@app.command("show")
def show(
    suggestion: str = typer.Option(..., "--suggestion"),
    tenant: str | None = typer.Option(None, "--tenant"),
) -> None:
    settings = _settings()
    scoped, pool = _scoped(settings, tenant)
    try:
        found = suggestions.get(scoped, suggestion)
        if found is None:
            raise typer.BadParameter(f"no suggestion {suggestion!r}")
        typer.echo(render(found))
    finally:
        pool.close()


def _decide(
    suggestion: str, status: suggestions.Status, by: str, note: str, tenant: str | None
) -> None:
    settings = _settings()
    scoped, pool = _scoped(settings, tenant)
    try:
        confirm.decide(scoped, suggestion, status, by, note)
        typer.echo(
            f"{suggestion}: {status} by {by}; CCP confirms it only when a later read sees it"
        )
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc
    finally:
        pool.close()


@app.command("accept")
def accept(
    suggestion: str = typer.Option(..., "--suggestion"),
    by: str = typer.Option(..., "--by"),
    note: str = typer.Option("", "--note"),
    tenant: str | None = typer.Option(None, "--tenant"),
) -> None:
    """Say the fix will be applied."""
    _decide(suggestion, "accepted", by, note, tenant)


@app.command("dismiss")
def dismiss(
    suggestion: str = typer.Option(..., "--suggestion"),
    by: str = typer.Option(..., "--by"),
    note: str = typer.Option(..., "--note", help="why it does not apply"),
    tenant: str | None = typer.Option(None, "--tenant"),
) -> None:
    _decide(suggestion, "dismissed", by, note, tenant)


@app.command("confirm")
def confirm_cmd(tenant: str | None = typer.Option(None, "--tenant")) -> None:
    """Read the new verdicts and confirm or reopen suggestions from them."""
    settings = _settings()
    scoped, pool = _scoped(settings, tenant)
    try:
        done = confirm.drain(scoped)
        typer.echo(f"confirmed {done.confirmed}, reopened {done.reopened}")
    finally:
        pool.close()


@app.command("metrics")
def metrics(tenant: str | None = typer.Option(None, "--tenant")) -> None:
    settings = _settings()
    scoped, pool = _scoped(settings, tenant)
    try:
        m = confirm.metrics(scoped)
        typer.echo(
            json.dumps(
                {
                    "by_status": m.by_status,
                    "median_hours_fail_to_confirmed": m.median_hours_to_confirmed,
                    "confirmed": m.confirmed,
                    "reopened": m.reopened,
                    "reopen_rate_30d": m.reopen_rate,
                },
                indent=2,
            )
        )
    finally:
        pool.close()
