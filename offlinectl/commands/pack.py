"""offlinectl pack — resolve and download all bundle dependencies."""

from __future__ import annotations

import shutil
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
from offlinectl.bundle.bundle import (
    BundleMetadata,
    BundleTaskMeta,
    bundle_dir_name,
    now_utc,
    write_meta,
)
from offlinectl.manifest.loader import load_manifest
from offlinectl.plugins.base import PackContext
from offlinectl.plugins.registry import registry

console = Console()
err_console = Console(stderr=True)

OFFLINECTL_VERSION = "0.1.0"


def pack_cmd(
    manifest: Path = typer.Argument(..., help="Path to bundle.yaml manifest"),
    output: Path = typer.Option(
        Path("."), "--output", "-o", help="Output directory for the bundle"
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Print what would be done, don't execute"
    ),
    only: str | None = typer.Option(None, "--only", help="Comma-separated list of plugins to run"),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Verbose output"),
) -> None:
    """Download and resolve all dependencies into a self-contained bundle directory."""
    # Load + validate manifest
    try:
        bundle = load_manifest(manifest)
    except (FileNotFoundError, ValueError) as exc:
        err_console.print(f"[bold red]ERROR:[/] {exc}")
        raise typer.Exit(1)

    name = bundle.metadata.name
    version = bundle.metadata.version
    targets = bundle.get_targets()
    tasks = bundle.get_tasks()

    # Filter by --only
    only_set: set[str] | None = None
    if only:
        only_set = {s.strip() for s in only.split(",")}

    console.print(f"\n[bold]Packing bundle:[/] [cyan]{name}[/] v[yellow]{version}[/]")
    if dry_run:
        console.print("[dim](dry-run mode — nothing will be executed)[/dim]\n")

    # Create bundle directory
    dir_name = bundle_dir_name(name, version)
    bundle_dir = (output / dir_name).resolve()
    bundle_dir.mkdir(parents=True, exist_ok=True)

    # Copy manifest into bundle
    shutil.copy2(str(manifest), str(bundle_dir / "bundle.yaml"))

    manifest_dir = manifest.parent.resolve()
    task_metas: list[BundleTaskMeta] = []
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
            continue

        console.print(rf"[bold]\[{i}/{len(tasks)}][/] [yellow]{plugin_name}[/]: {task.name}...")

        ctx = PackContext(
            bundle_dir=bundle_dir,
            manifest_dir=manifest_dir,
            dry_run=dry_run,
            verbose=verbose,
        )

        try:
            result = plugin.pack(task.config, ctx)
        except NotImplementedError as exc:
            err_console.print(f"  [red]✗ SKIP:[/] {exc}")
            task_metas.append(
                BundleTaskMeta(
                    name=task.name, plugin=plugin_name, status="skipped", artifact_count=0
                )
            )
            continue

        if result.success:
            console.print(f"  [green]✓[/] {result.message}")
            console.print(f"    [dim]artifacts: {len(result.artifacts)}[/dim]")
        else:
            overall_ok = False
            err_console.print(f"  [bold red]✗ FAILED:[/] {result.message}")
            for err in result.errors:
                err_console.print(f"    [red]{err}[/]")

        task_metas.append(
            BundleTaskMeta(
                name=task.name,
                plugin=plugin_name,
                status="packed" if result.success else "failed",
                artifact_count=len(result.artifacts),
            )
        )

    # Write bundle.meta.json
    if not dry_run:
        meta = BundleMetadata(
            name=name,
            version=version,
            created_at=now_utc(),
            offlinectl_version=OFFLINECTL_VERSION,
            targets={
                "distro": targets.distro,
                "codename": targets.codename,
                "arch": targets.arch,
            },
            tasks=task_metas,
        )
        write_meta(bundle_dir, meta)
        console.print(f"\n[bold green]Bundle ready:[/] {bundle_dir}")
    else:
        console.print(f"\n[dim]Would write bundle to:[/dim] {bundle_dir}")

    if not overall_ok:
        raise typer.Exit(1)
