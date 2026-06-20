import typer
import yaml
import questionary
from pathlib import Path
from typing import Optional
from rich import print as rprint
from rich.console import Console

from syncit.registry import get_catalog, resolve_subtask
from syncit.commands.pack import run_pack
from syncit.commands.up import run_up

console = Console()

DISTRO_CHOICES = ["Ubuntu", "Debian", "RHEL", "Rocky", "AlmaLinux"]
APT_DISTROS = {"Ubuntu", "Debian"}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _detect_codename() -> str:
    """Read the local OS codename from /etc/os-release, or return ''."""
    try:
        with open("/etc/os-release") as f:
            for line in f:
                if line.startswith("VERSION_CODENAME="):
                    return line.split("=", 1)[1].strip().strip('"')
    except OSError:
        pass
    return ""


def _load_manifest(path: Path) -> dict:
    with open(path) as f:
        return yaml.safe_load(f) or {}


def _dump_manifest(manifest: dict, path: Path) -> None:
    """Write YAML with repo URLs always on a single line (no PyYAML line-folding)."""
    import sys

    class _NoFoldDumper(yaml.Dumper):
        def __init__(self, stream, **kwargs):
            super().__init__(stream, **kwargs)
            # Override after super().__init__ — the only reliable way to stop
            # PyYAML from folding long scalars in write_plain / write_double_quoted.
            self.best_width = sys.maxsize

    with open(path, "w") as f:
        yaml.dump(
            manifest, f,
            Dumper=_NoFoldDumper,
            sort_keys=False,
            allow_unicode=True,
            default_flow_style=False,
        )







def get_package_choices(catalog: dict) -> list[questionary.Choice]:
    choices = []
    for key, data in catalog.items():
        desc = data.get("description", "")
        choices.append(questionary.Choice(title=f"{key:<15} ({desc})", value=key))
    return choices


def _add_subtasks(
    pkg_data: dict,
    plugin_type: str,
    pkg_version: str,
    codename: str,
    tasks: list,
) -> None:
    """Auto-add required subtasks; offer optional ones via checkbox."""
    subtasks = pkg_data.get("subtasks", {})
    if not subtasks:
        rprint("[yellow]Warning: catalog entry has no subtasks defined.[/yellow]")
        return

    optional_choices: list[questionary.Choice] = []

    for key, subtask_def in subtasks.items():
        label = subtask_def.get("label", key)
        required = subtask_def.get("required", False)

        resolved = resolve_subtask(subtask_def, plugin_type, pkg_version, codename)

        if resolved is None:
            if required:
                rprint(
                    f"[yellow]Warning: required subtask '{key}' has no template "
                    f"for plugin type '{plugin_type}' — skipping.[/yellow]"
                )
            continue

        if required:
            tasks.append(resolved)
            rprint(f"[green]Task added:[/] {resolved['name']} [dim](required)[/dim]")
        else:
            optional_choices.append(
                questionary.Choice(title=label, value=(resolved, resolved["name"]))
            )

    if optional_choices:
        selected = (
            questionary.checkbox(
                "Select optional components to include:",
                choices=optional_choices,
            ).ask()
            or []
        )
        for resolved, name in selected:
            tasks.append(resolved)
            rprint(f"[green]Task added:[/] {name}")


# ---------------------------------------------------------------------------
# Command
# ---------------------------------------------------------------------------

def create_cmd(
    manifest_file: Optional[Path] = typer.Argument(
        None,
        help="Path to an existing bundle.yaml to update. Omit to create a new one.",
        exists=False,   # allow non-existing path for new files
        file_okay=True,
        dir_okay=False,
    ),
) -> None:
    catalog = get_catalog()
    if not catalog:
        rprint("[yellow]Warning: Could not fetch catalog. Proceeding anyway.[/yellow]")
        catalog = {}

    # ── Update vs. create mode ────────────────────────────────────────────
    existing: dict = {}
    is_update = False
    if manifest_file and manifest_file.exists():
        is_update = True
        existing = _load_manifest(manifest_file)
        rprint(f"\n[bold cyan]Update mode:[/] loaded [green]{manifest_file}[/green]")
        existing_tasks = existing.get("spec", {}).get("tasks", [])
        if existing_tasks:
            rprint(f"[dim]Existing tasks ({len(existing_tasks)}):[/dim]")
            for t in existing_tasks:
                rprint(f"  [dim]· {t.get('name', '?')} ({t.get('plugin', '?')})[/dim]")
        rprint()

    # ── Metadata prompts (pre-filled from existing file in update mode) ───
    rprint("\n[bold]Bundle Metadata[/bold]")
    meta = existing.get("metadata", {})
    targets = existing.get("spec", {}).get("targets", {})

    bundle_name = questionary.text(
        "Bundle name:", default=meta.get("name", "")
    ).ask()
    if not bundle_name:
        raise typer.Exit()

    version = questionary.text("Version:", default=meta.get("version", "1.0.0")).ask()

    # Distro — pre-select existing value when updating
    existing_distro_raw = targets.get("distro", "")
    existing_distro = existing_distro_raw.capitalize() if existing_distro_raw else ""
    default_distro = existing_distro if existing_distro in DISTRO_CHOICES else "Ubuntu"
    distro_choice = questionary.select(
        "Target distro:",
        choices=DISTRO_CHOICES,
        default=default_distro,
    ).ask()

    # ── Codename with auto-detect + hints ────────────────────────────────
    if distro_choice in APT_DISTROS:
        detected = _detect_codename()
        existing_codename = targets.get("codename", "")

        # Priority: existing manifest value → detected from OS → fallback "noble"
        default_codename = existing_codename or detected or "noble"

        hints: list[str] = []
        if detected:
            tag = "[dim](recommended — detected on this machine)[/dim]"
            hints.append(f"  [cyan]{detected}[/cyan]  {tag}")
        if existing_codename and existing_codename != detected:
            hints.append(f"  [cyan]{existing_codename}[/cyan]  [dim](current in file)[/dim]")
        if hints:
            rprint("[dim]Codename hints:[/dim]")
            for h in hints:
                rprint(h)

        codename = questionary.text(
            "Codename:",
            default=default_codename,
        ).ask()
        plugin_type = "apt"
    else:
        codename = ""
        plugin_type = "dnf"

    # Arch — pre-select existing value when updating
    existing_arch = targets.get("arch", "amd64")
    arch = questionary.select(
        "Architecture:",
        choices=["amd64", "arm64"],
        default=existing_arch if existing_arch in ("amd64", "arm64") else "amd64",
    ).ask()

    # Carry forward existing tasks; new tasks appended in the loop below
    tasks: list = list(existing.get("spec", {}).get("tasks", []))

    # ── Task loop ─────────────────────────────────────────────────────────
    while True:
        rprint("\n[bold]Add a task[/bold]")
        action = questionary.select(
            "What next?",
            choices=["Search catalog", "Add empty task", "Done"],
        ).ask()

        if action == "Done":
            break

        if action == "Add empty task":
            task_name = questionary.text("Task name:").ask()
            tasks.append(
                {
                    "name": task_name,
                    "plugin": plugin_type,
                    "packages": ["<package_name>"],
                }
            )
            rprint(f"[green]Task added:[/] {task_name}")
            continue

        choices = get_package_choices(catalog)
        if not choices:
            rprint("[red]Catalog is empty.[/red]")
            continue

        pkg_key = questionary.select(
            "Select package:",
            choices=choices,
            use_indicator=True,
        ).ask()
        if not pkg_key:
            continue

        pkg_data = catalog[pkg_key]
        versions = pkg_data.get("versions", ["latest"])
        pkg_version = questionary.select("Version:", choices=versions).ask()

        _add_subtasks(pkg_data, plugin_type, pkg_version, codename, tasks)

    # ── Build and save manifest ───────────────────────────────────────────
    manifest = {
        "apiVersion": "syncit/v1",
        "kind": "Bundle",
        "metadata": {
            "name": bundle_name,
            "version": version,
        },
        "spec": {
            "targets": {
                "distro": distro_choice.lower(),
                "arch": arch,
            },
            "tasks": tasks,
        },
    }
    if codename:
        manifest["spec"]["targets"]["codename"] = codename

    rprint("\n")
    default_save = str(manifest_file) if manifest_file else "bundle.yaml"
    save_path = questionary.text("Save to:", default=default_save).ask()
    if not save_path:
        raise typer.Exit()

    save_file = Path(save_path)
    _dump_manifest(manifest, save_file)
    rprint(f"[green]{'Updated' if is_update else 'Saved'} {save_file}[/green]")

    # ── Run now? ──────────────────────────────────────────────────────────
    run_choice = questionary.select(
        "Run now?",
        choices=[
            questionary.Choice("Pack bundle locally", "pack"),
            questionary.Choice("Pack and apply remotely (syncit up)", "up"),
            questionary.Choice("Not yet", "none"),
        ],
    ).ask()

    if run_choice == "none":
        return

    rprint(f"\n[cyan]Starting syncit {run_choice}...[/cyan]")
    output_dir = Path("./bundles")

    if run_choice == "pack":
        try:
            run_pack(manifest=save_file, output=output_dir, dry_run=False, verbose=True)
        except Exception as e:
            rprint(f"[red]Pack failed:[/] {e}")
            raise typer.Exit(1)

    elif run_choice == "up":
        inventory_path = questionary.text(
            "Inventory file path:", default="inventory.yaml"
        ).ask()
        if not inventory_path or not Path(inventory_path).exists():
            rprint("[red]Valid inventory file is required.[/red]")
            raise typer.Exit(1)

        hosts = []
        try:
            with open(inventory_path) as f:
                inv = yaml.safe_load(f)
                if "hosts" in inv:
                    hosts = [h.get("name", h.get("host")) for h in inv["hosts"]]
        except Exception:
            pass

        if hosts:
            target_host = questionary.select("Target host:", choices=hosts).ask()
        else:
            target_host = questionary.text("Target host (IP/hostname):").ask()

        if not target_host:
            raise typer.Exit()

        try:
            run_up(
                manifest=save_file,
                inventory=Path(inventory_path),
                target=target_host,
            )
        except Exception as e:
            rprint(f"[red]Up failed:[/] {e}")
            raise typer.Exit(1)
