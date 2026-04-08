"""syncit validate — parse and validate a bundle.yaml manifest."""

from __future__ import annotations

from pathlib import Path

import typer
from rich.console import Console

# Import plugins to ensure they register themselves
import syncit.plugins.apt  # noqa: F401
import syncit.plugins.cargo  # noqa: F401
import syncit.plugins.go  # noqa: F401
import syncit.plugins.npm  # noqa: F401
import syncit.plugins.oci_image  # noqa: F401
import syncit.plugins.pip  # noqa: F401
from syncit.manifest.loader import load_manifest
from syncit.plugins.registry import registry

console = Console()
err_console = Console(stderr=True)


def validate_cmd(
    manifest: Path = typer.Argument(..., help="Path to bundle.yaml manifest"),
) -> None:
    """Validate a bundle.yaml manifest file and all its task specs."""
    # 1. Parse manifest
    try:
        bundle = load_manifest(manifest)
    except FileNotFoundError as exc:
        err_console.print(f"[bold red]ERROR:[/] {exc}")
        raise typer.Exit(1)
    except ValueError as exc:
        err_console.print(f"[bold red]ERROR:[/] {exc}")
        raise typer.Exit(1)

    console.print(f"[bold green]✓[/] Manifest schema valid — [cyan]{manifest}[/]")

    tasks = bundle.get_tasks()
    total_errors = 0

    for task in tasks:
        plugin_name = task.plugin

        # Look up plugin
        try:
            plugin = registry.get(plugin_name)
        except KeyError:
            err_console.print(
                f"[bold red]✗[/] [yellow]{task.name}[/]: "
                f"Unknown plugin '{plugin_name}'. "
                f"Available: {', '.join(registry.list_plugins())}"
            )
            total_errors += 1
            continue

        # Validate task spec
        errors = plugin.validate(task.config)
        if errors:
            for err in errors:
                err_console.print(f"[bold red]✗[/] [yellow]{task.name}[/]: {err}")
            total_errors += len(errors)
        else:
            console.print(f"[bold green]✓[/] [yellow]{task.name}[/] ([cyan]{plugin_name}[/]) — OK")

    if total_errors:
        err_console.print(f"\n[bold red]{total_errors} validation error(s) found.[/]")
        raise typer.Exit(1)
    else:
        console.print(f"\n[bold green]All {len(tasks)} task(s) valid.[/]")
