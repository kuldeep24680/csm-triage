"""Command line entry point: ``python -m triage <command>``."""
from __future__ import annotations

import json
import logging
from datetime import date, timedelta

import typer

from .config import settings

app = typer.Typer(add_completion=False, no_args_is_help=True, pretty_exceptions_enable=False)


@app.callback()
def _init():
    from .observability import setup
    setup("triage-agent")


def _d(s: str | None) -> date:
    if s:
        return date.fromisoformat(s)
    return date.today() if settings.use_wall_clock else settings.sim_end_date


@app.command()
def migrate():
    """Apply SQL migrations."""
    from .db import migrate as m
    typer.echo(f"applied: {m()}")


@app.command("reset-db")
def reset_db(yes: bool = typer.Option(False, "--yes")):
    """Drop and recreate the schema (dev only)."""
    if not yes:
        raise typer.Abort()
    from .db import migrate as m, reset_schema
    reset_schema()
    typer.echo(f"applied: {m()}")


@app.command()
def generate(end: str = typer.Option(None), years: int = settings.sim_years, seed: int = settings.seed):
    """Generate the synthetic landing zone (deterministic for a given seed)."""
    from .synth.generator import generate as g
    metas = g(settings.data_dir, _d(end), years, seed)
    typer.echo(json.dumps(metas, indent=2))


@app.command()
def ingest(tenant: str = typer.Option(None), up_to: str = typer.Option(None)):
    """Load landing files that arrived on or before --up-to, then resolve identities."""
    from .db import tenant_tx
    from .ingest.identity import resolve_identities
    from .ingest.loader import ingest_tenant, register_tenants
    tenants = [t for t in register_tenants() if not tenant or t == tenant]
    for t in tenants:
        st = ingest_tenant(t, _d(up_to))
        with tenant_tx(t) as conn:
            st["identity"] = resolve_identities(conn)
        typer.echo(json.dumps({t: st}, indent=2, default=str))


@app.command()
def train(as_of: str = typer.Option(None), activate: bool = True):
    """Build point-in-time training set across tenants, fit, evaluate, register."""
    from .model.train import train_and_register
    res = train_and_register(_d(as_of), activate=activate)
    typer.echo(json.dumps(res, indent=2, default=str))


@app.command()
def run(tenant: str = typer.Option(None), as_of: str = typer.Option(None), trigger: str = "manual",
        resume: str = typer.Option(None, help="run_id of a failed/abandoned run to continue from its failed step"),
        no_retry: bool = typer.Option(False, help="disable the single automatic retry")):
    """One unattended agent run: ingest -> resolve -> features -> score -> diff -> briefs."""
    from .agent.runner import resume_run, run_all
    res = [resume_run(resume)] if resume else run_all(_d(as_of), tenant=tenant, trigger=trigger, auto_retry=not no_retry)
    typer.echo(json.dumps(res, indent=2, default=str))
    if any(r["status"] in ("failed", "error") for r in res):
        raise typer.Exit(1)   # non-zero exit -> systemd OnFailure= fires the alert unit


@app.command()
def watchdog(as_of: str = typer.Option(None), stale_minutes: int = 15, no_start: bool = False):
    """Morning check: abandon stuck runs, alert, and start today's run if it is missing."""
    from .agent.runner import watchdog as w
    typer.echo(json.dumps(w(_d(as_of), stale_minutes, start_missing=not no_start), indent=2, default=str))


@app.command()
def alert(message: str):
    """Send an alert (used by systemd OnFailure= and to test the webhook)."""
    from .alerts import send
    send(message)


@app.command()
def backfill(weeks: int = 8, as_of: str = typer.Option(None), tenant: str = typer.Option(None)):
    """Replay weekly runs so 'what changed since last week' has history."""
    from .agent.runner import run_all
    end = _d(as_of)
    for k in range(weeks - 1, -1, -1):
        d = end - timedelta(weeks=k)
        res = run_all(d, tenant=tenant, trigger="backfill")
        typer.echo(json.dumps({"as_of": d, "runs": res}, default=str))


@app.command("seed-users")
def seed_users(password: str = "triage-demo"):
    """Create app logins: one per CRM user (CSM) plus a manager per tenant."""
    from .api.auth import seed_users as s
    typer.echo(json.dumps(s(password), indent=2))


@app.command()
def serve(host: str = "0.0.0.0", port: int = 8000, reload: bool = False):
    import uvicorn
    uvicorn.run("triage.api.main:app", host=host, port=port, reload=reload)


if __name__ == "__main__":
    app()
