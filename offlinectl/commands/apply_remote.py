"""offlinectl apply-remote — zero-dependency remote apply via SSH."""

from __future__ import annotations

import subprocess
from pathlib import Path

import typer
from rich.console import Console

console = Console()
err_console = Console(stderr=True)


def apply_remote_cmd(
    bundle_path: Path = typer.Option(..., "--bundle", "-b", help="Path to local bundle archive"),
    inventory: Path | None = typer.Option(None, "--inventory", "-i", help="Path to inventory YAML file"),
    target: str | None = typer.Option(None, "--target", "-t", help="Target host or group from inventory"),
    print_script: bool = typer.Option(False, "--print-script", help="Print the generated apply.sh and exit"),
) -> None:
    """Run zero-dependency remote apply on targeted VMs via SSH."""
    import shutil
    import tempfile

    from offlinectl.inventory.loader import load_inventory, resolve_targets
    from offlinectl.bundle.archive import detect_bundle
    from offlinectl.manifest.loader import load_manifest
    from offlinectl.plugins.registry import registry

    if not bundle_path.exists():
        err_console.print(f"[red]Error: Bundle path '{bundle_path}' does not exist.[/red]")
        raise typer.Exit(1)

    if not print_script:
        if not inventory or not target:
            err_console.print("[red]Error: --inventory and --target are required unless --print-script is used.[/red]")
            raise typer.Exit(1)

        if not shutil.which("ssh") or not shutil.which("scp"):
            err_console.print(
                "[red]Error: 'ssh' and 'scp' commands are required on the jumphost.[/red]"
            )
            raise typer.Exit(1)

        try:
            inv = load_inventory(inventory)
            hosts = resolve_targets(inv, target)
        except Exception as e:
            err_console.print(f"[red]Error loading inventory or target: {e}[/red]")
            raise typer.Exit(1)

    # Generate apply.sh
    try:
        with detect_bundle(bundle_path) as actual_bundle_dir:
            bundle_manifest = load_manifest(actual_bundle_dir / "bundle.yaml")
            tasks = bundle_manifest.get_tasks()

            script_lines = [
                "#!/usr/bin/env bash",
                "set -euo pipefail",
                "",
                "ARCHIVE=$1",
                'BUNDLE_DEST=$(dirname "$ARCHIVE")',
                'BUNDLE_DIR="$BUNDLE_DEST/extracted"',
                "",
                'echo "[offlinectl] Extracting bundle archive..."',
                'mkdir -p "$BUNDLE_DIR"',
                'tar -xf "$ARCHIVE" -C "$BUNDLE_DIR"',
                '# Normalize structure: if there is only one directory in the root, move its contents up',
                'ENTRY_COUNT=$(ls -1 "$BUNDLE_DIR" | wc -l)',
                'if [ "$ENTRY_COUNT" -eq 1 ] && [ -d "$BUNDLE_DIR"/* ]; then',
                '  SUBDIR=$(ls -1 "$BUNDLE_DIR")',
                '  echo "[offlinectl] Normalizing bundle structure (found top-level \'$SUBDIR\')..."',
                '  mv "$BUNDLE_DIR/$SUBDIR"/* "$BUNDLE_DIR/" 2>/dev/null || true',
                '  mv "$BUNDLE_DIR/$SUBDIR"/.[!.]* "$BUNDLE_DIR/" 2>/dev/null || true',
                '  rmdir "$BUNDLE_DIR/$SUBDIR" 2>/dev/null || true',
                'fi',
                '# Debug: list extracted contents if it fails later',
                'trap \'if [ $? -ne 0 ]; then echo "[offlinectl] ERROR: Deployment failed. Structure of $BUNDLE_DIR:"; ls -R "$BUNDLE_DIR"; fi\' EXIT',
                "",
            ]

            for task in tasks:
                plugin = registry.get(task.plugin)
                if hasattr(plugin, "render_apply_sh"):
                    snippet = plugin.render_apply_sh(task.config, task.plugin)
                    if snippet:
                        script_lines.append(snippet)
                else:
                    err_console.print(
                        f"[yellow]Warning: Plugin {task.plugin} does not support zero-dependency remote apply.[/yellow]"
                    )

            apply_sh_content = "\n".join(script_lines)
    except Exception as e:
        err_console.print(f"[red]Error generating apply.sh: {e}[/red]")
        raise typer.Exit(1)

    if print_script:
        print(apply_sh_content)
        return

    with tempfile.NamedTemporaryFile("w", delete=False, prefix="apply-", suffix=".sh") as f:
        f.write(apply_sh_content)
        apply_sh_local = f.name

    try:
        errors = []
        for host_id, host in hosts:
            console.print(
                f"\nApplying bundle remotely on [bold cyan]{host_id}[/bold cyan] ({host.host})..."
            )

            # 1. SCP the bundle and script
            scp_args = ["scp"]
            if host.ssh_key:
                key_path = Path(host.ssh_key).expanduser()
                scp_args.extend(["-i", str(key_path)])

            dest = f"{host.user}@{host.host}:{host.bundle_dest}"
            scp_args.extend([str(bundle_path), apply_sh_local, dest])

            console.print(f"Transferring {bundle_path.name} and apply.sh to {host_id}...")
            scp_res = subprocess.run(scp_args, capture_output=True, text=True)
            if scp_res.returncode != 0:
                err_console.print(f"[red]SCP failed: {scp_res.stderr}[/red]")
                errors.append(host_id)
                continue

            # 2. SSH run
            ssh_args = ["ssh"]
            if host.ssh_key:
                ssh_args.extend(["-i", str(key_path)])
            ssh_args.append(f"{host.user}@{host.host}")

            remote_bundle = f"{host.bundle_dest.rstrip('/')}/{bundle_path.name}"
            remote_script = f"{host.bundle_dest.rstrip('/')}/{Path(apply_sh_local).name}"

            remote_cmd = f"sudo bash {remote_script} {remote_bundle}"
            ssh_args.append(remote_cmd)

            console.print(f"Running zero-dependency apply on {host_id}...")
            host_failed = False
            with subprocess.Popen(
                ssh_args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True
            ) as proc:
                if proc.stdout:
                    for line in iter(proc.stdout.readline, ""):
                        console.print(f"[dim]{host_id}:[/dim] {line.rstrip()}")
                proc.wait()

                if proc.returncode != 0:
                    err_console.print(
                        f"[red]Failed to apply remotely on {host_id}. SSH exit code {proc.returncode}[/red]"
                    )
                    # If it failed with 127 it means bash couldn't find a command or we are hitting 
                    # the old code that tried to run 'offlinectl'.
                    if proc.returncode == 127:
                         err_console.print("[yellow]Hint: If you see 'offlinectl: command not found', ensure you are using 'uv run offlinectl' on the jumphost to run the latest code.[/yellow]")
                    errors.append(host_id)
                    host_failed = True
                else:
                    console.print(f"[green]Successfully applied on {host_id}.[/green]")

            # Step 8: Clean up apply.sh from target regardless of success/failure
            cleanup_args = ["ssh"]
            if host.ssh_key:
                cleanup_args.extend(["-i", str(key_path)])
            cleanup_args.append(f"{host.user}@{host.host}")
            cleanup_args.append(f"rm -f {remote_script}")
            subprocess.run(cleanup_args, capture_output=True)  # best-effort, ignore errors

        if errors:
            err_console.print(
                f"\n[red]Remote apply completed with errors on {len(errors)} host(s).[/red]"
            )
            raise typer.Exit(1)
    finally:
        Path(apply_sh_local).unlink(missing_ok=True)
