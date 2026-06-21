"""syncit pack — resolve and download all bundle dependencies."""

from __future__ import annotations

import re
import shutil
from pathlib import Path

import typer
from rich.console import Console

# Import plugins to ensure they register themselves
import syncit.plugins.apt  # noqa: F401
import syncit.plugins.cargo  # noqa: F401
import syncit.plugins.dnf  # noqa: F401
import syncit.plugins.file  # noqa: F401
import syncit.plugins.go  # noqa: F401
import syncit.plugins.npm  # noqa: F401
import syncit.plugins.oci_image  # noqa: F401
import syncit.plugins.pip  # noqa: F401
from syncit.bundle.bundle import (
    BundleMetadata,
    BundleTaskMeta,
    bundle_dir_name,
    now_utc,
    write_meta,
)
from syncit.manifest.loader import load_manifest
from syncit.plugins.base import PackContext
from syncit.plugins.registry import registry

console = Console()
err_console = Console(stderr=True)

from syncit import __version__ as SYNCIT_VERSION


def _task_slug(name: str) -> str:
    """Convert a task name to a safe directory slug, e.g. 'Install CRI-O' → 'install-cri-o'."""
    slug = name.lower()
    slug = re.sub(r"[^a-z0-9]+", "-", slug)
    return slug.strip("-")


def run_pack(
    manifest: Path,
    output: Path,
    dry_run: bool = False,
    only: str | None = None,
    format: str = "dir",
    verbose: bool = False,
    no_cache: bool = False,
) -> Path | None:
    """Download and resolve all dependencies and write them into a self-contained bundle directory."""
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
    import tempfile

    if format != "dir" and not dry_run:
        tmp_dir_path = Path(tempfile.mkdtemp(prefix="syncit-pack-"))
        bundle_dir = tmp_dir_path / dir_name
    else:
        tmp_dir_path = None
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
            task_slug=_task_slug(task.name),
            dry_run=dry_run,
            verbose=verbose,
            no_cache=no_cache,
        )

        try:
            # 1. Validate the task config first
            validation_errors = plugin.validate(task.config)
            if validation_errors:
                # Check if we can auto-install any missing dependencies
                missing_pkg = None
                install_cmd = None
                
                # Check for dpkg-scanpackages (dpkg-dev)
                if any("dpkg-scanpackages" in err for err in validation_errors):
                    if shutil.which("apt-get"):
                        missing_pkg = "dpkg-dev"
                        install_cmd = ["apt-get", "install", "-y", "dpkg-dev"]
                # Check for createrepo_c
                elif any("createrepo_c" in err for err in validation_errors):
                    missing_pkg = "createrepo_c"
                    if shutil.which("dnf"):
                        install_cmd = ["dnf", "install", "-y", "createrepo_c"]
                    elif shutil.which("yum"):
                        install_cmd = ["yum", "install", "-y", "createrepo_c"]
                    elif shutil.which("apt-get"):
                        install_cmd = ["apt-get", "install", "-y", "createrepo-c"]
                # Check for skopeo
                elif any("skopeo" in err for err in validation_errors):
                    missing_pkg = "skopeo"
                    if shutil.which("apt-get"):
                        install_cmd = ["apt-get", "install", "-y", "skopeo"]
                    elif shutil.which("dnf"):
                        install_cmd = ["dnf", "install", "-y", "skopeo"]
                    elif shutil.which("yum"):
                        install_cmd = ["yum", "install", "-y", "skopeo"]

                if missing_pkg and install_cmd:
                    import questionary
                    if questionary.confirm(
                        f"Command for '{plugin_name}' is missing. Would you like to install package '{missing_pkg}' automatically? (requires sudo)",
                        default=True
                    ).ask():
                        console.print(f"[cyan]Installing '{missing_pkg}'...[/cyan]")
                        try:
                            from syncit.plugins.base import run_privileged
                            res = run_privileged(install_cmd, capture_output=True, text=True)
                            if res.returncode == 0:
                                console.print(f"[green]Successfully installed '{missing_pkg}'![/green]")
                                # Re-run validation
                                validation_errors = plugin.validate(task.config)
                            else:
                                err_console.print(f"[red]Failed to install '{missing_pkg}': {res.stderr.strip()}[/red]")
                        except Exception as e:
                            err_console.print(f"[red]Error during install: {e}[/red]")

            if validation_errors:
                err_console.print(f"  [bold red]✗ VALIDATION FAILED:[/] {task.name}")
                for err in validation_errors:
                    err_console.print(f"    [red]{err}[/]")
                overall_ok = False
                task_metas.append(
                    BundleTaskMeta(
                        name=task.name, plugin=plugin_name, status="failed", artifact_count=0
                    )
                )
                continue

            # 2. Pack the task
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
    final_result_path: Path | None = bundle_dir
    if not dry_run:
        meta = BundleMetadata(
            name=name,
            version=version,
            created_at=now_utc(),
            syncit_version=SYNCIT_VERSION,
            targets={
                "distro": targets.distro,
                "codename": targets.codename,
                "arch": targets.arch,
            },
            tasks=task_metas,
        )
        write_meta(bundle_dir, meta)

        if format != "dir":
            from syncit.bundle.archive import pack_archive

            archive_target = (output / dir_name).resolve()
            archive_target.parent.mkdir(parents=True, exist_ok=True)
            final_result_path = pack_archive(bundle_dir, archive_target, format)
            if tmp_dir_path:
                shutil.rmtree(tmp_dir_path, ignore_errors=True)
            console.print(f"\n[bold green]Archive ready:[/] {final_result_path}")
        else:
            console.print(f"\n[bold green]Bundle ready:[/] {bundle_dir}")
    else:
        out_msg = str(output / dir_name)
        if format != "dir":
            ext = ".zip" if format == "zip" else ".tar.gz"
            out_msg += ext
        console.print(f"\n[dim]Would write bundle to:[/dim] {out_msg}")
        final_result_path = Path(out_msg)

    if not overall_ok:
        if tmp_dir_path:
            shutil.rmtree(tmp_dir_path, ignore_errors=True)
        raise typer.Exit(1)

    return final_result_path


def pack_cmd(
    manifest: Path = typer.Argument(..., help="Path to bundle.yaml manifest"),
    output: Path = typer.Option(
        Path("."), "--output", "-o", help="Output directory for the bundle"
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Print what would be done, don't execute"
    ),
    only: str | None = typer.Option(None, "--only", help="Comma-separated list of plugins to run"),
    format: str = typer.Option("dir", "--format", help="Output format: dir, tar.gz, zip"),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Verbose output"),
    no_cache: bool = typer.Option(
        False, "--no-cache", help="Do not use local cache, force download"
    ),
) -> None:
    """Download and resolve all dependencies and write them into a self-contained bundle directory."""
    run_pack(
        manifest=manifest,
        output=output,
        dry_run=dry_run,
        only=only,
        format=format,
        verbose=verbose,
        no_cache=no_cache,
    )
