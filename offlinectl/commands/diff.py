"""offlinectl diff — compare two bundle versions."""

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
from offlinectl.manifest.loader import load_manifest
from offlinectl.plugins.registry import registry

console = Console()
err_console = Console(stderr=True)


def diff_cmd(
    bundle_v1: Path = typer.Argument(..., help="Path to the older bundle directory"),
    bundle_v2: Path = typer.Argument(..., help="Path to the newer bundle directory"),
) -> None:
    """Compare two bundle versions and show what changed per plugin."""
    bundle_v1 = bundle_v1.resolve()
    bundle_v2 = bundle_v2.resolve()

    for path in (bundle_v1, bundle_v2):
        if not path.exists():
            err_console.print(f"[bold red]ERROR:[/] Bundle directory not found: {path}")
            raise typer.Exit(1)

    # Load manifests from inside the bundles
    def _load(bundle_path: Path):
        manifest_file = bundle_path / "bundle.yaml"
        try:
            return load_manifest(manifest_file)
        except (FileNotFoundError, ValueError) as exc:
            err_console.print(f"[bold red]ERROR:[/] {exc}")
            raise typer.Exit(1)

    manifest_v1 = _load(bundle_v1)
    manifest_v2 = _load(bundle_v2)

    # Build task lookup by plugin name for v1
    def _task_map(manifest) -> dict[str, dict]:
        result: dict[str, dict] = {}
        for task in manifest.get_tasks():
            result[task.plugin] = task.config
        return result

    v1_map = _task_map(manifest_v1)
    v2_map = _task_map(manifest_v2)

    # All plugins mentioned in either bundle
    all_plugins = sorted(set(v1_map) | set(v2_map))

    console.print(
        f"\n[bold]Diffing:[/] "
        f"[yellow]{manifest_v1.metadata.name} v{manifest_v1.metadata.version}[/]"
        f"  →  "
        f"[cyan]{manifest_v2.metadata.name} v{manifest_v2.metadata.version}[/]\n"
    )

    any_changes = False

    for plugin_name in all_plugins:
        old_spec = v1_map.get(plugin_name)
        new_spec = v2_map.get(plugin_name)

        if new_spec is None:
            # Plugin removed entirely
            console.print(f"[bold]{plugin_name}:[/]")
            console.print("  [red]- (plugin removed entirely)[/]")
            any_changes = True
            continue

        try:
            plugin = registry.get(plugin_name)
        except KeyError:
            console.print(f"[bold]{plugin_name}:[/]")
            console.print("  [dim]Unknown plugin — cannot diff[/dim]")
            continue

        try:
            diff = plugin.diff(old_spec, new_spec)
        except NotImplementedError:
            console.print(f"[bold]{plugin_name}:[/]")
            console.print("  [dim]diff not implemented for this plugin[/dim]")
            continue

        has_changes = bool(diff.added or diff.removed or diff.updated)
        any_changes = any_changes or has_changes

        console.print(f"[bold]{plugin_name}:[/]")

        for item in diff.added:
            console.print(f"  [green]+ {item}[/]")
        for item in diff.removed:
            console.print(f"  [red]- {item}[/]")
        for item in diff.updated:
            console.print(f"  [yellow]~ {item}[/]")
        if not has_changes:
            console.print("  [dim](no changes)[/dim]")
        if diff.unchanged and not has_changes:
            pass  # suppress unchanged list unless verbose
        console.print()

    if not any_changes:
        console.print("[bold green]Bundles are identical.[/]")
