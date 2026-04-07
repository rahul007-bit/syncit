"""offlinectl apply-remote — trigger apply command remotely via SSH."""

from __future__ import annotations

import subprocess
from pathlib import Path

import typer
from rich.console import Console

console = Console()
err_console = Console(stderr=True)


def apply_remote_cmd(
    bundle_filename: str = typer.Option(..., "--bundle", help="Filename of the transferred bundle"),
    inventory: Path = typer.Option(..., "--inventory", "-i", help="Path to inventory YAML file"),
    target: str = typer.Option(..., "--target", "-t", help="Target host or group from inventory"),
) -> None:
    """Run `offlinectl apply` on targeted remote VMs via SSH."""
    import shutil

    from offlinectl.inventory.loader import load_inventory, resolve_targets

    if not shutil.which("ssh"):
        err_console.print(
            "[red]Error: 'ssh' command requires native ssh utilities which are not available on this path.[/red]"
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
            f"Applying bundle remotely on [bold cyan]{host_id}[/bold cyan] ({host.host})..."
        )

        args = ["ssh"]
        if host.ssh_key:
            key_path = Path(host.ssh_key).expanduser()
            args.extend(["-i", str(key_path)])

        args.append(f"{host.user}@{host.host}")

        # Build the exact remote command
        # Ensure we join bundle_dest and bundle_filename safely.
        bundle_full_dest = str(Path(host.bundle_dest) / bundle_filename)
        remote_cmd = f"offlinectl apply {bundle_full_dest} --state-file {host.state_file}"
        args.append(remote_cmd)

        result = subprocess.run(args, capture_output=True, text=True)
        if result.returncode != 0:
            err_console.print(
                f"[red]Failed to apply remotely on {host_id}. SSH exit code {result.returncode}[/red]"
            )
            err_console.print(f"[red]Stderr: {result.stderr}[/red]")
            err_console.print(f"[red]Stdout: {result.stdout}[/red]")
            errors.append(host_id)
        else:
            console.print(f"[green]Successfully applied on {host_id}.[/green]")
            console.print(result.stdout)

    if errors:
        err_console.print(
            f"[red]Remote apply completed with errors on {len(errors)} host(s).[/red]"
        )
        raise typer.Exit(1)
