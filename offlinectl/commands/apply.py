"""offlinectl apply — install a bundle onto the offline VM."""

from __future__ import annotations

from pathlib import Path

import typer
from rich.console import Console

# Import plugins to ensure they register themselves
import offlinectl.plugins.apt  # noqa: F401
import offlinectl.plugins.cargo  # noqa: F401
import offlinectl.plugins.go  # noqa: F401
import offlinectl.plugins.npm  # noqa: F401
import offlinectl.plugins.oci_image  # noqa: F401
import offlinectl.plugins.pip  # noqa: F401
from offlinectl.bundle.bundle import compute_task_checksum, now_utc, read_meta
from offlinectl.bundle.state import DEFAULT_STATE_FILE, AppliedTask, load_state, save_state
from offlinectl.manifest.loader import load_manifest
from offlinectl.plugins.base import ApplyContext
from offlinectl.plugins.registry import registry

console = Console()
err_console = Console(stderr=True)


def apply_cmd(
    bundle_dir: Path = typer.Argument(..., help="Path to bundle directory or archive"),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Show what would be applied, don't execute"
    ),
    force: bool = typer.Option(False, "--force", help="Re-apply even if state says already done"),
    only: str | None = typer.Option(None, "--only", help="Comma-separated plugin names to run"),
    state_file: Path = typer.Option(DEFAULT_STATE_FILE, "--state-file", help="Path to state.json"),
    continue_on_error: bool = typer.Option(
        False, "--continue-on-error", help="Don't stop on plugin failure"
    ),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Verbose output"),
) -> None:
    """Apply a bundle onto this (offline) VM, configuring local package sources."""
    bundle_dir = bundle_dir.resolve()

    if not bundle_dir.exists():
        err_console.print(f"[bold red]ERROR:[/] Bundle path not found: {bundle_dir}")
        raise typer.Exit(1)

    from offlinectl.bundle.archive import detect_bundle

    try:
        with detect_bundle(bundle_dir) as actual_bundle_dir:
            _run_apply(
                actual_bundle_dir, dry_run, force, only, state_file, continue_on_error, verbose
            )
    except ValueError as e:
        err_console.print(f"[bold red]ERROR:[/] {e}")
        raise typer.Exit(1)


def _run_apply(
    bundle_dir: Path,
    dry_run: bool,
    force: bool,
    only: str | None,
    state_file: Path,
    continue_on_error: bool,
    verbose: bool,
) -> None:

    # Load bundle metadata
    try:
        meta = read_meta(bundle_dir)
    except FileNotFoundError as exc:
        err_console.print(f"[bold red]ERROR:[/] {exc}")
        raise typer.Exit(1)

    # Load the manifest from inside the bundle
    bundle_manifest_path = bundle_dir / "bundle.yaml"
    try:
        bundle_manifest = load_manifest(bundle_manifest_path)
    except (FileNotFoundError, ValueError) as exc:
        err_console.print(f"[bold red]ERROR:[/] Cannot read bundle manifest: {exc}")
        raise typer.Exit(1)

    tasks = bundle_manifest.get_tasks()
    state = load_state(state_file)

    only_set: set[str] | None = None
    if only:
        only_set = {s.strip() for s in only.split(",")}

    console.print(f"\n[bold]Applying bundle:[/] [cyan]{meta.name}[/] v[yellow]{meta.version}[/]")
    if dry_run:
        console.print("[dim](dry-run mode — nothing will be changed)[/dim]\n")

    overall_ok = True

    for i, task in enumerate(tasks, start=1):
        plugin_name = task.plugin

        if only_set and plugin_name not in only_set:
            console.print(f"[{i}/{len(tasks)}] [dim]Skipping {task.name} ({plugin_name})[/dim]")
            continue

        try:
            plugin = registry.get(plugin_name)
        except KeyError:
            err_console.print(
                f"[bold red][{i}/{len(tasks)}] ERROR:[/] Unknown plugin '{plugin_name}'"
            )
            overall_ok = False
            if not continue_on_error:
                raise typer.Exit(1)
            continue

        # Idempotence: check state checksum
        checksum = compute_task_checksum(bundle_dir, plugin_name)
        existing = state.get_task(task.name)

        if existing and existing.checksum == checksum and not force:
            console.print(
                f"[{i}/{len(tasks)}] [dim]✓ {task.name} ({plugin_name}) — already applied, skipping[/dim]"
            )
            continue

        console.print(rf"[bold]\[{i}/{len(tasks)}][/] [yellow]{plugin_name}[/]: {task.name}...")

        ctx = ApplyContext(
            bundle_dir=bundle_dir,
            state_file=state_file,
            dry_run=dry_run,
            verbose=verbose,
            force=force,
        )

        try:
            result = plugin.apply(task.config, ctx)
        except NotImplementedError as exc:
            err_console.print(f"  [red]✗ NOT IMPLEMENTED:[/] {exc}")
            overall_ok = False
            if not continue_on_error:
                raise typer.Exit(1)
            continue

        if result.success:
            console.print(f"  [green]✓[/] {result.message}")

            if not dry_run:
                applied_task = AppliedTask(
                    name=task.name,
                    plugin=plugin_name,
                    bundle_version=meta.version,
                    applied_at=now_utc(),
                    checksum=checksum,
                )
                state.upsert_task(applied_task)
        else:
            overall_ok = False
            err_console.print(f"  [bold red]✗ FAILED:[/] {result.message}")
            for err in result.errors:
                err_console.print(f"    [red]{err}[/]")

            if not continue_on_error:
                err_console.print(
                    "\n[red]Stopping. Use --continue-on-error to proceed despite failures.[/]"
                )
                raise typer.Exit(1)

    # Persist state
    if not dry_run:
        state.last_bundle = f"{meta.name}-{meta.version}"
        state.last_applied_at = now_utc()
        save_state(state_file, state)
        console.print(f"\n[bold green]State saved:[/] {state_file}")

    if overall_ok:
        console.print("[bold green]All tasks applied successfully.[/]")
    else:
        err_console.print("[bold red]Some tasks failed.[/]")
        raise typer.Exit(1)
