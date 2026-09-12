"""``ccp copilot --tenant acme [--ask "..."] [--yes]``: the REPL and its single-shot form."""

from __future__ import annotations

from collections.abc import Callable

import typer

from ccp.config import Settings
from ccp.copilot import runner
from ccp.evaluators.tier2 import Providers
from ccp.observability import steps
from ccp.storage import db
from ccp.storage.scoped import ScopedDB
from ccp.tenancy import TenantContext

EXIT_WORDS = frozenset({"exit", "quit", "q"})


def _turn(session: runner.Session, question: str, confirm: Callable[[], bool]) -> None:
    turn = session.ask(question)
    typer.echo(turn.answer.text)
    for c in turn.answer.citations:
        typer.echo(f'  [{c.chunk_id}] "{c.quote[:120]}"')
    for event in turn.rejected:
        typer.echo(f"  ! {event}", err=True)
    if not turn.pending:
        return
    typer.echo(runner.describe(turn))
    if confirm():
        opened = session.confirm(turn)
        typer.echo(f"opened approval item(s): {', '.join(opened.approval_ids) or 'none'}")
        if opened.investigation_ids:
            typer.echo(f"investigation(s) opened first: {', '.join(opened.investigation_ids)}")
    else:
        typer.echo("No approval item was opened.")


def copilot(
    tenant: str | None = typer.Option(None, "--tenant"),
    ask: str | None = typer.Option(None, "--ask", help="Single-shot question; no REPL"),
    yes: bool = typer.Option(False, "--yes", help="With --ask: confirm requested actions"),
) -> None:
    """The Compliance Copilot: questions over this tenant's evidence, state, and verdicts."""
    settings = Settings()
    steps.configure(settings)
    chosen = tenant or settings.ccp_tenant
    if not chosen:
        raise typer.BadParameter("pass --tenant or set CCP_TENANT")
    ctx = TenantContext.for_id(settings, chosen)
    providers = Providers.from_settings(settings)
    if providers.llm is None:
        typer.echo("the Copilot needs ANTHROPIC_API_KEY", err=True)
        raise typer.Exit(code=2)
    pool = db.app_pool(settings)
    try:
        with runner.Session(ScopedDB(pool, ctx), settings, providers) as session:
            if ask is not None:
                _turn(session, ask, lambda: yes)
                if not yes:
                    typer.echo("(re-run with --yes to confirm requested actions)", err=True)
                return
            typer.echo(
                f"Compliance Copilot for {chosen}. Ask a question; 'exit' to leave."
                " Nothing here changes anything without a reviewer."
            )
            while True:
                try:
                    line = typer.prompt(">", prompt_suffix=" ")
                except (EOFError, KeyboardInterrupt, typer.Abort):
                    typer.echo("")
                    break
                question = line.strip()
                if not question:
                    continue
                if question.lower() in EXIT_WORDS:
                    break
                _turn(
                    session,
                    question,
                    lambda: typer.confirm("Open these approval items?", default=False),
                )
    finally:
        pool.close()
