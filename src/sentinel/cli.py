"""Sentinel's CLI entrypoint. Commands are added as each agent lands —
see ARCHITECTURE.md for the build order. `sentinel --help` always reflects
what's actually implemented; don't add a command stub here before its
agent module exists."""

from __future__ import annotations

import typer

app = typer.Typer(
    name="sentinel",
    help="DataHub Sentinel — agents that catch data breakage before it ships.",
    no_args_is_help=True,
)


@app.command()
def version() -> None:
    """Print the installed Sentinel version."""
    from importlib.metadata import version as _version

    try:
        typer.echo(_version("datahub-sentinel"))
    except Exception:
        typer.echo("0.1.0-dev")


if __name__ == "__main__":
    app()
