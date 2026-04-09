"""syncit exec — run arbitrary shell commands on remote hosts via SSH."""

from __future__ import annotations

import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import typer
from rich.console import Console

console = Console()
err_console = Console(stderr=True)


def exec_cmd(
    command: list[str] = typer.Argument(..., help="Command to run"),
    inventory: Path = typer.Option(..., "-i", "--inventory"),
    target: str | None = typer.Option(None, "-t", "--target"),
    group: str | None = typer.Option(None, "-g", "--group"),
    all_hosts: bool = typer.Option(False, "--all"),
    sudo: bool = typer.Option(False, "--sudo"),
    timeout: int = typer.Option(30, "--timeout"),
) -> None:
    """Run an arbitrary shell command on remote hosts via SSH."""
    from syncit.inventory.loader import load_inventory, resolve_targets

    import shutil

    if not shutil.which("ssh"):
        err_console.print("[red]Error: 'ssh' command is required on the jumphost.[/red]")
        raise typer.Exit(1)

    if not all_hosts and not target and not group:
        err_console.print("[red]Error: Must provide one of --target, --group, or --all.[/red]")
        raise typer.Exit(1)

    try:
        inv = load_inventory(inventory)
    except Exception as e:
        err_console.print(f"[red]Error loading inventory: {e}[/red]")
        raise typer.Exit(1)

    # Resolve target hosts
    if all_hosts:
        hosts = list(inv.hosts.items())
    elif target:
        try:
            hosts = resolve_targets(inv, target)
        except ValueError as e:
            err_console.print(f"[red]Error: {e}[/red]")
            raise typer.Exit(1)
    else:
        assert group is not None
        try:
            hosts = resolve_targets(inv, group)
        except ValueError as e:
            err_console.print(f"[red]Error: {e}[/red]")
            raise typer.Exit(1)

    if not hosts:
        err_console.print("[yellow]No hosts to execute on.[/yellow]")
        raise typer.Exit(0)

    results: dict[str, tuple[int, str, str]] = {}

    def run_on_host(host_id: str, host_obj: "Host") -> None:  # type: ignore[name-defined]
        ssh_base = _build_ssh_base(host_obj)
        cmd_str = " ".join(command)
        if sudo:
            remote_cmd = ["sudo", "bash", "-c", cmd_str]
        else:
            remote_cmd = ["bash", "-c", cmd_str]
        full_cmd = ssh_base + remote_cmd

        try:
            proc = subprocess.run(
                full_cmd,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            results[host_id] = (proc.returncode, proc.stdout, proc.stderr)

            # Print output immediately for this host (escape brackets for Rich)
            for line in proc.stdout.splitlines():
                console.print(rf"[cyan]\[{host_id}][/cyan] {line}")
            for line in proc.stderr.splitlines():
                err_console.print(rf"[cyan]\[{host_id}][/cyan] [red]{line}[/red]")

        except subprocess.TimeoutExpired:
            results[host_id] = (124, "", f"Command timed out after {timeout}s")
            err_console.print(
                f"[cyan][{host_id}][/cyan] [red]Command timed out after {timeout}s[/red]"
            )
        except Exception as e:
            results[host_id] = (1, "", str(e))
            err_console.print(f"[cyan][{host_id}][/cyan] [red]{e}[/red]")

    with ThreadPoolExecutor(max_workers=len(hosts)) as executor:
        futures = {
            executor.submit(run_on_host, host_id, host_obj): host_id for host_id, host_obj in hosts
        }
        for future in as_completed(futures):
            future.result()  # propagate exceptions

    # Print summary
    failed = 0
    for host_id, (returncode, _, _) in results.items():
        if returncode == 0:
            console.print(f"✓ [cyan]{host_id}[/cyan] — exit {returncode}")
        else:
            err_console.print(f"✗ [cyan]{host_id}[/cyan] — exit {returncode}")
            failed += 1

    if failed:
        raise typer.Exit(1)


def _build_ssh_base(host: "Host") -> list[str]:  # type: ignore[name-defined]
    """Build the base SSH command arguments for a host."""
    from pathlib import Path

    args = ["ssh"]
    if host.ssh_key:
        key_path = Path(host.ssh_key).expanduser()
        args.extend(["-i", str(key_path)])
    args.append(f"{host.user}@{host.host}")
    return args
