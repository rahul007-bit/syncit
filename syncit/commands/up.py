"""syncit up — pack a bundle and immediately apply it remotely."""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

import typer
from rich.console import Console

from syncit.commands.apply import run_apply
from syncit.commands.pack import run_pack

console = Console()
err_console = Console(stderr=True)


def up_cmd(
    manifest: Path = typer.Argument(..., help="Path to bundle.yaml manifest"),
    inventory: Path = typer.Option(..., "-i", "--inventory", help="Path to inventory YAML file"),
    target: str | None = typer.Option(None, "-t", "--target", help="Target host from inventory"),
    group: str | None = typer.Option(None, "-g", "--group", help="Target group from inventory"),
    all_hosts: bool = typer.Option(False, "--all", help="Apply to all hosts in inventory"),
    format: str = typer.Option("tar.gz", "--format", help="Archive format to use"),
    verbose: bool = typer.Option(False, "-v", "--verbose", help="Verbose output"),
    no_cache: bool = typer.Option(
        False, "--no-cache", help="Do not use local cache, force download"
    ),
) -> None:
    """Pack a bundle and immediately apply it remotely to the specified targets."""
    from syncit.inventory.loader import load_inventory, resolve_targets

    # 1. Resolve targets first so we don't pack if inventory/target is invalid
    if not all_hosts and not target and not group:
        err_console.print("[red]Error: Must provide one of --target, --group, or --all.[/red]")
        raise typer.Exit(1)

    try:
        inv = load_inventory(inventory)
        if all_hosts:
            apply_target = "all"
        else:
            apply_target = str(target or group)
            # Validate target exists
            resolve_targets(inv, apply_target)
    except Exception as e:
        err_console.print(f"[red]Error validating inventory/target: {e}[/red]")
        raise typer.Exit(1)

    # 2. Pack to a temporary directory
    tmp_dir = Path(tempfile.mkdtemp(prefix="syncit-up-"))
    try:
        console.print(f"[bold blue]Step 1: Packing bundle...[/bold blue]")
        bundle_path = run_pack(
            manifest=manifest,
            output=tmp_dir,
            dry_run=False,
            format=format,
            verbose=verbose,
            no_cache=no_cache,
        )

        if not bundle_path or not bundle_path.exists():
            err_console.print("[red]Error: Packing failed, no bundle produced.[/red]")
            raise typer.Exit(1)

        # 3. Apply remotely
        console.print(f"\n[bold blue]Step 2: Applying bundle remotely...[/bold blue]")
        # We need to map 'all' or 'group' back to the way run_apply expects them.
        # run_apply in apply.py currently takes 'target' which it passes to resolve_targets.
        # So we just pass apply_target.
        run_apply(
            bundle_path=bundle_path,
            inventory=inventory,
            target=apply_target,
            print_script=False,
        )

    finally:
        # 4. Cleanup
        if tmp_dir.exists():
            shutil.rmtree(tmp_dir, ignore_errors=True)

    console.print(f"\n[bold green]✓ 'syncit up' completed successfully.[/bold green]")
