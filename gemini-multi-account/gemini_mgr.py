#!/usr/bin/env python3
"""
gemini-mgr — Multi-account Gemini CLI manager

Commands:
  config   — set Google OAuth client_id / client_secret
  add      — add a new Google account via OAuth flow
  list     — list all accounts with status
  launch   — launch Gemini CLI with a specific account + proxy
  status   — check quota/usage for all (or one) account
  set-proxy — bind a proxy to an account
  disable  — disable an account (keeps credentials)
  enable   — re-enable a disabled account
  remove   — delete an account permanently

Token rotation (for scripts / API proxy use):
  pick     — pick the best account alias using P2C rotation logic
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import click
from rich.console import Console
from rich.table import Table

console = Console()
CONFIG_FILE = Path(__file__).parent / "config.json"


# ──────────────────────────────────────────────
# CLI root
# ──────────────────────────────────────────────
@click.group()
def cli():
    """Gemini CLI multi-account manager with token rotation."""


# ──────────────────────────────────────────────
# config
# ──────────────────────────────────────────────
@cli.command()
@click.option("--client-id", required=True, help="Google OAuth 2.0 Client ID")
@click.option("--client-secret", required=True, help="Google OAuth 2.0 Client Secret")
def config(client_id, client_secret):
    """Save Google OAuth client credentials (from Google Cloud Console)."""
    data = {}
    if CONFIG_FILE.exists():
        data = json.loads(CONFIG_FILE.read_text())
    data["client_id"] = client_id
    data["client_secret"] = client_secret
    CONFIG_FILE.write_text(json.dumps(data, indent=2))
    console.print("[green]Config saved.[/green]")


# ──────────────────────────────────────────────
# add
# ──────────────────────────────────────────────
@cli.command()
@click.argument("alias")
@click.option("--proxy", default="", help="Proxy URL, e.g. socks5://127.0.0.1:1080")
def add(alias, proxy):
    """Add a new Google account via browser OAuth flow."""
    from src.accounts import create_new_meta, get_account, save_account
    from src.oauth import _load_client_config, run_oauth_flow

    if get_account(alias):
        console.print(f"[yellow]Account '{alias}' already exists. Use a different alias.[/yellow]")
        sys.exit(1)

    try:
        cfg = _load_client_config()
    except FileNotFoundError as e:
        console.print(f"[red]{e}[/red]")
        sys.exit(1)

    console.print(f"[bold]Adding account:[/bold] {alias}")

    try:
        creds, user_info = run_oauth_flow(
            cfg["client_id"],
            cfg["client_secret"],
            proxy=proxy or None,
        )
    except Exception as e:
        console.print(f"[red]OAuth failed: {e}[/red]")
        sys.exit(1)

    email = user_info.get("email", "unknown")
    meta = create_new_meta(alias, email, proxy)
    save_account(meta, creds)
    console.print(f"[green]Account added:[/green] {alias} ({email})")


# ──────────────────────────────────────────────
# list
# ──────────────────────────────────────────────
@cli.command("list")
def list_cmd():
    """List all accounts and their status."""
    from src.accounts import list_accounts, load_creds

    accounts = list_accounts()
    if not accounts:
        console.print("[yellow]No accounts found. Run: gemini-mgr add <alias>[/yellow]")
        return

    table = Table(title="Gemini Accounts", show_lines=True)
    table.add_column("Alias", style="cyan", no_wrap=True)
    table.add_column("Email")
    table.add_column("Proxy")
    table.add_column("Token", justify="center")
    table.add_column("Quota %", justify="right")
    table.add_column("Tier")
    table.add_column("Health", justify="right")
    table.add_column("Status")

    for meta in accounts:
        creds = load_creds(meta.alias)
        if creds is None:
            token_status = "[red]missing[/red]"
        elif creds.is_expired():
            token_status = "[yellow]expired[/yellow]"
        else:
            token_status = "[green]ok[/green]"

        quota_str = f"{meta.remaining_quota}%" if meta.remaining_quota is not None else "-"
        tier = meta.subscription_tier or "-"
        health = f"{meta.health_score:.1f}"
        status = "[red]disabled[/red]" if meta.disabled else "[green]active[/green]"
        proxy_display = meta.proxy or "-"

        table.add_row(meta.alias, meta.email, proxy_display, token_status, quota_str, tier, health, status)

    console.print(table)


# ──────────────────────────────────────────────
# launch
# ──────────────────────────────────────────────
@cli.command()
@click.argument("alias")
@click.option("--bin", "gemini_bin", default="gemini", help="Path to gemini binary")
@click.argument("gemini_args", nargs=-1)
def launch(alias, gemini_bin, gemini_args):
    """Launch Gemini CLI for a specific account."""
    from src.launcher import launch as do_launch
    code = do_launch(alias, list(gemini_args), gemini_bin)
    sys.exit(code)


# ──────────────────────────────────────────────
# status
# ──────────────────────────────────────────────
@cli.command()
@click.argument("alias", required=False)
def status(alias):
    """Check quota/usage for all accounts (or a single one)."""
    from src.accounts import get_account, list_accounts
    from src.usage import check_account, check_all

    if alias:
        meta = get_account(alias)
        if meta is None:
            console.print(f"[red]Account '{alias}' not found.[/red]")
            sys.exit(1)
        accounts = [meta]
    else:
        accounts = [a for a in list_accounts() if not a.disabled]

    if not accounts:
        console.print("[yellow]No active accounts.[/yellow]")
        return

    console.print("[bold]Checking quota...[/bold]")
    results = check_all(accounts)

    table = Table(title="Quota Status", show_lines=True)
    table.add_column("Alias", style="cyan")
    table.add_column("Email")
    table.add_column("Status", justify="center")
    table.add_column("Quota %", justify="right")
    table.add_column("Reset in")
    table.add_column("Error")

    for r in results:
        if r.status == "ok":
            status_cell = "[green]ok[/green]"
        elif r.status == "rate_limited":
            status_cell = "[yellow]limited[/yellow]"
        elif r.status == "expired":
            status_cell = "[yellow]expired[/yellow]"
        else:
            status_cell = "[red]error[/red]"

        quota_str = f"{r.remaining_pct}%" if r.remaining_pct is not None else "-"
        reset_str = f"{int(r.reset_in_sec)}s" if r.reset_in_sec else "-"
        err_str = r.error or ""

        table.add_row(r.alias, r.email, status_cell, quota_str, reset_str, err_str)

    console.print(table)


# ──────────────────────────────────────────────
# set-proxy
# ──────────────────────────────────────────────
@cli.command("set-proxy")
@click.argument("alias")
@click.argument("proxy_url")
def set_proxy(alias, proxy_url):
    """Bind a proxy to an account (e.g. socks5://127.0.0.1:1080)."""
    from src.accounts import get_account, save_meta
    meta = get_account(alias)
    if meta is None:
        console.print(f"[red]Account '{alias}' not found.[/red]")
        sys.exit(1)
    meta.proxy = proxy_url
    save_meta(meta)
    console.print(f"[green]Proxy set for '{alias}':[/green] {proxy_url}")


@cli.command("clear-proxy")
@click.argument("alias")
def clear_proxy(alias):
    """Remove proxy from an account (use direct connection)."""
    from src.accounts import get_account, save_meta
    meta = get_account(alias)
    if meta is None:
        console.print(f"[red]Account '{alias}' not found.[/red]")
        sys.exit(1)
    meta.proxy = ""
    save_meta(meta)
    console.print(f"[green]Proxy cleared for '{alias}'.[/green]")


# ──────────────────────────────────────────────
# disable / enable
# ──────────────────────────────────────────────
@cli.command()
@click.argument("alias")
def disable(alias):
    """Disable an account (skipped in rotation)."""
    from src.accounts import get_account, save_meta
    meta = get_account(alias)
    if meta is None:
        console.print(f"[red]Account '{alias}' not found.[/red]")
        sys.exit(1)
    meta.disabled = True
    save_meta(meta)
    console.print(f"[yellow]Account '{alias}' disabled.[/yellow]")


@cli.command()
@click.argument("alias")
def enable(alias):
    """Re-enable a disabled account."""
    from src.accounts import get_account, save_meta
    meta = get_account(alias)
    if meta is None:
        console.print(f"[red]Account '{alias}' not found.[/red]")
        sys.exit(1)
    meta.disabled = False
    save_meta(meta)
    console.print(f"[green]Account '{alias}' enabled.[/green]")


# ──────────────────────────────────────────────
# remove
# ──────────────────────────────────────────────
@cli.command()
@click.argument("alias")
@click.confirmation_option(prompt="This will permanently delete the account. Continue?")
def remove(alias):
    """Delete an account and its credentials permanently."""
    from src.accounts import delete_account
    if delete_account(alias):
        console.print(f"[green]Account '{alias}' removed.[/green]")
    else:
        console.print(f"[red]Account '{alias}' not found.[/red]")
        sys.exit(1)


# ──────────────────────────────────────────────
# pick  (token rotation for scripts)
# ──────────────────────────────────────────────
@cli.command()
@click.option("--session", default=None, help="Session ID for sticky routing")
@click.option("--model", default=None, help="Target model (for protected-model check)")
@click.option(
    "--mode",
    default="balance",
    type=click.Choice(["cache-first", "balance", "performance-first"]),
    help="Scheduling mode",
)
def pick(session, model, mode):
    """
    Pick the best account alias using P2C rotation logic.
    Prints the alias to stdout — useful in shell scripts.

    Example:
        ALIAS=$(gemini-mgr pick --session my-session)
        gemini-mgr launch $ALIAS
    """
    from src.token_manager import SchedulingMode, TokenManager

    mode_map = {
        "cache-first": SchedulingMode.CACHE_FIRST,
        "balance": SchedulingMode.BALANCE,
        "performance-first": SchedulingMode.PERFORMANCE_FIRST,
    }

    tm = TokenManager(mode=mode_map[mode])
    count = tm.load_all()
    if count == 0:
        console.print("[red]No active accounts loaded.[/red]", file=sys.stderr)
        sys.exit(1)

    token = tm.get_token(session_id=session, target_model=model)
    if token is None:
        console.print("[red]No available account (all rate-limited or empty).[/red]", file=sys.stderr)
        sys.exit(1)

    # Print alias to stdout (clean, for shell capture)
    print(token.alias)


# ──────────────────────────────────────────────
# Entry point
# ──────────────────────────────────────────────
if __name__ == "__main__":
    cli()
