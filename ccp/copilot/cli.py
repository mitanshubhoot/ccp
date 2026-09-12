"""``ccp copilot --tenant acme [--ask "..."] [--yes]``: the REPL and its single-shot form."""

from __future__ import annotations

import re
from collections.abc import Callable

import typer

from ccp.config import Settings
from ccp.copilot import runner
from ccp.evaluators.tier2 import Providers
from ccp.ingest.consumers import ParkedError
from ccp.observability import steps
from ccp.storage import db
from ccp.storage.scoped import ScopedDB
from ccp.tenancy import TenantContext

EXIT_WORDS = frozenset({"exit", "quit", "q"})
_CONTROL = re.compile(
    r"\x1b\[[0-?]*[ -/]*[@-~]"  # CSI sequences (colours, cursor moves)
    r"|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)"  # OSC sequences (titles, hyperlinks, clipboard)
    r"|[\x00-\x08\x0b-\x1f\x7f]"  # other C0 controls except newline and tab
)


def printable(text: str) -> str:
    """Model output and evidence quotes reach the terminal without escape sequences or
    lone surrogates: a document cannot retitle the window or write the clipboard."""
    return _CONTROL.sub("", text).encode("utf-8", "replace").decode("utf-8")


def _turn(session: runner.Session, question: str, confirm: Callable[[], bool]) -> None:
    streamed: list[str] = []

    def show(token: str) -> None:
        streamed.append(token)
        typer.echo(printable(token), nl=False)

    turn = session.ask(question, on_token=show)
    prose = "".join(streamed).strip()
    if prose:
        typer.echo("")
    final = printable(turn.answer.text)
    if not prose:
        typer.echo(final)
    elif final.startswith(prose):
        rest = final[len(prose) :].strip()
        if rest:
            typer.echo(rest)
    else:  # the contract rewrote the answer after generation
        typer.echo(final)
    for c in turn.answer.citations:
        typer.echo(f'  [{c.chunk_id}] "{printable(c.quote[:120])}"')
    for event in turn.rejected:
        typer.echo(f"  ! {printable(event)}", err=True)
    if not turn.pending:
        return
    typer.echo(printable(runner.describe(turn)))
    if confirm():
        try:
            opened = session.confirm(turn)
        except ParkedError as exc:  # the Investigator it needed is over the daily cap
            typer.echo(f"parked: {exc}", err=True)
            return
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
