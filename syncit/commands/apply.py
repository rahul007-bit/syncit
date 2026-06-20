"""syncit apply-remote — zero-dependency remote apply via SSH with Smart Apply state backend."""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import typer
from rich.console import Console

console = Console()
err_console = Console(stderr=True)


def _task_slug(name: str) -> str:
    """Convert a task name to a safe directory slug."""
    slug = name.lower()
    slug = re.sub(r"[^a-z0-9]+", "-", slug)
    return slug.strip("-")


def build_ssh_cmd(host) -> list[str]:
    """Build the base SSH command list (with optional -i for ssh_key)."""
    cmd = ["ssh"]
    if host.ssh_key:
        key_path = Path(host.ssh_key).expanduser()
        cmd.extend(["-i", str(key_path)])
    cmd.append(f"{host.user}@{host.host}")
    return cmd


def run_apply(
    bundle_path: Path,
    inventory: Path | None = None,
    target: str | None = None,
    print_script: bool = False,
) -> None:
    """Core logic to run zero-dependency remote apply on targeted VMs via SSH."""
    import shutil

    from syncit.bundle.bundle import compute_task_checksum
    from syncit.bundle.archive import detect_bundle
    from syncit.inventory.loader import load_inventory, resolve_targets
    from syncit.manifest.loader import load_manifest
    from syncit.plugins.registry import registry
    from syncit.state import RemoteState, TaskState

    if not bundle_path.exists():
        err_console.print(f"[red]Error: Bundle path '{bundle_path}' does not exist.[/red]")
        raise typer.Exit(1)

    if not print_script:
        if not inventory or not target:
            err_console.print(
                "[red]Error: --inventory and --target are required unless --print-script is used.[/red]"
            )
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

    # Load manifest (needed for both --print-script and apply paths)
    try:
        with detect_bundle(bundle_path) as actual_bundle_dir:
            bundle_manifest = load_manifest(actual_bundle_dir / "bundle.yaml")
            tasks = bundle_manifest.get_tasks()
    except Exception as e:
        err_console.print(f"[red]Error reading bundle manifest: {e}[/red]")
        raise typer.Exit(1)

    # --print-script: generate and print the full apply.sh, then exit
    if print_script:
        script_lines = [
            "#!/usr/bin/env bash",
            "set -euo pipefail",
            "",
            "ARCHIVE=$1",
            'BUNDLE_DEST=$(dirname "$ARCHIVE")',
            'BUNDLE_DIR="$BUNDLE_DEST/extracted"',
            "",
            'echo "[syncit] Extracting bundle archive..."',
            'sudo rm -rf "$BUNDLE_DIR"',
            'sudo mkdir -p "$BUNDLE_DIR"',
            'sudo tar -xf "$ARCHIVE" -C "$BUNDLE_DIR"',
            "# Normalize structure: if there is exactly one top-level entry and it is a directory, move its contents up",
            'ENTRY_COUNT=$(ls -1 "$BUNDLE_DIR" | wc -l)',
            'if [ "$ENTRY_COUNT" -eq 1 ] && [ -d "$BUNDLE_DIR/$(ls -1 "$BUNDLE_DIR")" ]; then',
            '  SUBDIR=$(ls -1 "$BUNDLE_DIR")',
            '  echo "[syncit] Normalizing bundle structure (top-level: $SUBDIR)..."',
            '  sudo mv "$BUNDLE_DIR/$SUBDIR"/* "$BUNDLE_DIR/" 2>/dev/null || true',
            '  sudo mv "$BUNDLE_DIR/$SUBDIR"/.[!.]* "$BUNDLE_DIR/" 2>/dev/null || true',
            '  sudo rmdir "$BUNDLE_DIR/$SUBDIR" 2>/dev/null || true',
            "fi",
            "# Fail-fast: print structure on any error",
            'trap \'if [ $? -ne 0 ]; then echo "[syncit] ERROR: Deployment failed. Structure of $BUNDLE_DIR:"; ls -R "$BUNDLE_DIR"; fi\' EXIT',
            "",
        ]

        for task in tasks:
            plugin = registry.get(task.plugin)
            if hasattr(plugin, "render_apply_sh"):
                bundle_subdir = _task_slug(task.name)
                snippet = plugin.render_apply_sh(task.config, bundle_subdir)
                if snippet:
                    script_lines.append(snippet)
            else:
                err_console.print(
                    f"[yellow]Warning: Plugin {task.plugin} does not support zero-dependency remote apply.[/yellow]"
                )

        print("\n".join(script_lines))
        return

    # --- Smart Apply execution ---
    errors = []
    for host_id, host in hosts:
        console.print(
            f"\nApplying bundle remotely on [bold cyan]{host_id}[/bold cyan] ({host.host})..."
        )

        # Step A: SCP the bundle archive to target
        scp_args = ["scp"]
        if host.ssh_key:
            key_path = Path(host.ssh_key).expanduser()
            scp_args.extend(["-i", str(key_path)])

        dest = f"{host.user}@{host.host}:{host.bundle_dest}"
        scp_args.extend([str(bundle_path), dest])

        console.print(f"Transferring {bundle_path.name} to {host_id}...")
        scp_res = subprocess.run(scp_args, capture_output=True, text=True)
        if scp_res.returncode != 0:
            err_console.print(f"[red]SCP failed: {scp_res.stderr}[/red]")
            errors.append(host_id)
            continue

        # Step A (cont.): SSH — extract archive and ensure state directory exists
        import shlex
        remote_bundle = f"{host.bundle_dest.rstrip('/')}/{bundle_path.name}"
        bundle_extracted_dir = f"{host.bundle_dest.rstrip('/')}/extracted"

        extract_cmd = (
            f"sudo rm -rf {shlex.quote(bundle_extracted_dir)} && "
            f"sudo mkdir -p {shlex.quote(bundle_extracted_dir)} && "
            f"sudo tar -xf {shlex.quote(remote_bundle)} -C {shlex.quote(bundle_extracted_dir)} --strip-components=1 && "
            f"sudo mkdir -p /opt/syncit/"
        )

        ssh_extract = build_ssh_cmd(host) + [extract_cmd]
        extract_res = subprocess.run(ssh_extract, capture_output=True, text=True)
        if extract_res.returncode != 0:
            err_console.print(f"[red]Extract failed on {host_id}: {extract_res.stderr}[/red]")
            errors.append(host_id)
            continue

        # Step B: Fetch remote state
        state_file_path = host.state_file
        fetch_cmd = build_ssh_cmd(host) + ["sudo", "cat", state_file_path]
        try:
            state_res = subprocess.run(fetch_cmd, capture_output=True, text=True)
            if state_res.returncode == 0 and state_res.stdout.strip():
                remote_state = RemoteState.model_validate_json(state_res.stdout.strip())
            else:
                remote_state = RemoteState()
        except Exception:
            remote_state = RemoteState()

        # Step C & D: Iterate tasks with diff check and streaming execution
        with detect_bundle(bundle_path) as actual_bundle_dir:
            for task in tasks:
                plugin = registry.get(task.plugin)
                if not hasattr(plugin, "render_apply_sh"):
                    err_console.print(
                        f"[yellow]Warning: Plugin {task.plugin} does not support zero-dependency remote apply. Skipping.[/yellow]"
                    )
                    continue

                # Step C: Diff check — skip if unchanged & successful
                task_checksum = compute_task_checksum(actual_bundle_dir, _task_slug(task.name))
                prev_state = remote_state.applied_tasks.get(task.name)
                if (
                    prev_state
                    and prev_state.checksum == task_checksum
                    and prev_state.status == "success"
                ):
                    console.print(f"  [green]✓ SKIP:[/] {task.name} (Unchanged & Successful)")
                    continue

                # Step D: Stream the task execution via bash -s
                bundle_subdir = _task_slug(task.name)
                snippet = plugin.render_apply_sh(task.config, bundle_subdir)
                full_script = (
                    f"#!/usr/bin/env bash\n"
                    f"set -euo pipefail\n"
                    f"BUNDLE_DIR='{bundle_extracted_dir}'\n\n"
                    f"{snippet}"
                )

                console.print(f"  → Running task: {task.name}")
                ssh_cmd = build_ssh_cmd(host) + ["sudo", "bash", "-s"]

                proc = subprocess.Popen(
                    ssh_cmd,
                    stdin=subprocess.PIPE,
                    stdout=sys.stdout,
                    stderr=sys.stderr,
                    text=True,
                )

                # Write the script to stdin and close it so the remote bash knows the script is complete
                assert proc.stdin is not None  # guaranteed by stdin=PIPE
                proc.stdin.write(full_script)
                proc.stdin.close()

                # Wait for the remote execution to finish
                proc.wait()

                # Step E: Atomic state push
                if proc.returncode == 0:
                    remote_state.applied_tasks[task.name] = TaskState(
                        checksum=task_checksum, status="success"
                    )
                    state_json = remote_state.model_dump_json()
                    push_cmd = build_ssh_cmd(host) + [
                        "sudo",
                        "tee",
                        state_file_path,
                        ">",
                        "/dev/null",
                    ]
                    subprocess.run(push_cmd, input=state_json, text=True, check=True)
                    console.print(f"  [green]✓ SUCCESS:[/] {task.name}")
                else:
                    remote_state.applied_tasks[task.name] = TaskState(
                        checksum=task_checksum, status="failed"
                    )
                    state_json = remote_state.model_dump_json()
                    push_cmd = build_ssh_cmd(host) + [
                        "sudo",
                        "tee",
                        state_file_path,
                        ">",
                        "/dev/null",
                    ]
                    try:
                        subprocess.run(push_cmd, input=state_json, text=True, check=True)
                    except subprocess.CalledProcessError:
                        err_console.print(
                            f"[red]Warning: Failed to push state after task failure on {host_id}.[/red]"
                        )
                    console.print(f"  [red]✗ FAILED:[/] {task.name} - Aborting apply pipeline.")
                    sys.exit(1)

        console.print(f"[green]Successfully applied on {host_id}.[/green]")

    if errors:
        err_console.print(
            f"\n[red]Remote apply completed with errors on {len(errors)} host(s).[/red]"
        )
        raise typer.Exit(1)


def apply_cmd(
    bundle_path: Path = typer.Option(..., "--bundle", "-b", help="Path to local bundle archive"),
    inventory: Path | None = typer.Option(
        None, "--inventory", "-i", help="Path to inventory YAML file"
    ),
    target: str | None = typer.Option(
        None, "--target", "-t", help="Target host or group from inventory"
    ),
    print_script: bool = typer.Option(
        False, "--print-script", help="Print the generated apply.sh and exit"
    ),
) -> None:
    """Run zero-dependency remote apply on targeted VMs via SSH."""
    run_apply(
        bundle_path=bundle_path,
        inventory=inventory,
        target=target,
        print_script=print_script,
    )
