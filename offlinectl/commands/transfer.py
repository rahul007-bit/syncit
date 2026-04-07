"""offlinectl transfer — transfer a bundle to a destination VM."""

from __future__ import annotations

import subprocess
from pathlib import Path

import typer
from rich.console import Console

console = Console()
err_console = Console(stderr=True)


def transfer_cmd(
    bundle_path: Path = typer.Argument(..., help="Path to bundle archive or directory"),
    inventory: Path = typer.Option(..., "--inventory", "-i", help="Path to inventory YAML file"),
    target: str = typer.Option(..., "--target", "-t", help="Target host or group from inventory"),
) -> None:
    """Transfer a bundle to offline VMs via SCP."""
    from offlinectl.inventory.loader import load_inventory, resolve_targets

    if not bundle_path.exists():
        err_console.print(f"[red]Error: Bundle path '{bundle_path}' does not exist.[/red]")
        raise typer.Exit(1)

    import shutil

    if not shutil.which("scp"):
        err_console.print(
            "[red]Error: 'scp' command requires native ssh utilities which are not available on this path.[/red]"
        )
        raise typer.Exit(1)

    try:
        inv = load_inventory(inventory)
        hosts = resolve_targets(inv, target)
    except Exception as e:
        err_console.print(f"[red]Error loading inventory or target: {e}[/red]")
        raise typer.Exit(1)

    errors = []

    for host_id, host in hosts:
        console.print(
            f"Transferring {bundle_path.name} to [bold cyan]{host_id}[/bold cyan] ({host.host})..."
        )

        args = ["scp"]
        if host.ssh_key:
            # properly expand ~ just in case
            key_path = Path(host.ssh_key).expanduser()
            args.extend(["-i", str(key_path)])

        dest = f"{host.user}@{host.host}:{host.bundle_dest}"
        args.extend([str(bundle_path), dest])

        result = subprocess.run(args, capture_output=True, text=True)
        if result.returncode != 0:
            err_console.print(
                f"[red]Failed to transfer to {host_id}. SCP exit code {result.returncode}[/red]"
            )
            err_console.print(f"[red]Stderr: {result.stderr}[/red]")
            errors.append(host_id)
        else:
            console.print(f"[green]Successfully transferred to {host_id}.[/green]")

    if errors:
        err_console.print(f"[red]Transfer completed with errors on {len(errors)} host(s).[/red]")
        raise typer.Exit(1)
