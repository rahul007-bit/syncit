"""syncit — Air-gap bundle orchestrator CLI entrypoint."""

from __future__ import annotations

import typer
from rich.console import Console

from syncit.commands.apply import apply_cmd
from syncit.commands.diff import diff_cmd
from syncit.commands.exec_cmd import exec_cmd
from syncit.commands.pack import pack_cmd
from syncit.commands.up import up_cmd
from syncit.commands.validate import validate_cmd

__version__ = "0.3.0"

app = typer.Typer(
    name="syncit",
    help="Air-gap bundle orchestrator — pack dependencies online, apply offline.",
    add_completion=True,
    rich_markup_mode="rich",
)

console = Console()


def version_callback(value: bool) -> None:
    if value:
        console.print(f"syncit {__version__}")
        raise typer.Exit()


@app.callback()
def main(
    version: bool | None = typer.Option(
        None,
        "--version",
        "-V",
        callback=version_callback,
        is_eager=True,
        help="Print version and exit",
    ),
) -> None:
    """syncit — air-gap bundle orchestrator for Linux environments."""


# Register commands directly (no sub-Typers — avoids double-name UX issue)
app.command("validate", help="Validate a bundle.yaml manifest file.")(validate_cmd)
app.command("pack", help="Download and bundle all dependencies (run on online VM).")(pack_cmd)
app.command("apply", help="Run zero-dependency remote apply on targeted VMs via SSH.")(apply_cmd)
app.command("up", help="Pack a bundle and immediately apply it remotely.")(up_cmd)
app.command("diff", help="Compare two bundle versions and show what changed.")(diff_cmd)
app.command("exec", help="Run a shell command on remote hosts via SSH.")(exec_cmd)


if __name__ == "__main__":
    app()
