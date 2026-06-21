import json
import sys
import typer
import yaml
import questionary
from pathlib import Path
from typing import Optional
from rich import print as rprint
from rich.console import Console

from syncit.registry import (
    get_catalog,
    resolve_subtask,
    save_to_user_catalog,
    save_to_project_catalog,
)
from syncit.commands.pack import run_pack
from syncit.commands.up import run_up

console = Console()

DISTRO_CHOICES = ["Ubuntu", "Debian", "RHEL", "Rocky", "AlmaLinux"]
APT_DISTROS = {"Ubuntu", "Debian"}


# ---------------------------------------------------------------------------
# Internal helpers
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


def _detect_distro() -> str:
    """Read the local OS distro name from /etc/os-release, or return ''."""
    try:
        id_val = ""
        id_like_val = ""
        with open("/etc/os-release") as f:
            for line in f:
                if line.startswith("ID="):
                    id_val = line.split("=", 1)[1].strip().strip('"').strip("'").lower()
                elif line.startswith("ID_LIKE="):
                    id_like_val = line.split("=", 1)[1].strip().strip('"').strip("'").lower()
        
        mapping = {
            "ubuntu": "Ubuntu",
            "debian": "Debian",
            "rhel": "RHEL",
            "rocky": "Rocky",
            "almalinux": "AlmaLinux",
        }
        if id_val in mapping:
            return mapping[id_val]
            
        for token in id_like_val.split():
            if token in mapping:
                return mapping[token]
    except OSError:
        pass
    return ""


def _load_manifest(path: Path) -> dict:
    with open(path) as f:
        return yaml.safe_load(f) or {}


def _dump_manifest(manifest: dict, path: Path) -> None:
    """Write YAML with repo URLs always on a single line (no PyYAML line-folding)."""

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


def _print_catalog_table(catalog: dict) -> None:
    """Print a compact table of all catalog entries so the user can see what's available."""
    from rich.table import Table
    table = Table(box=None, padding=(0, 2), show_header=True, header_style="bold dim")
    table.add_column("Package", style="cyan", no_wrap=True)
    table.add_column("Category", style="dim", no_wrap=True)
    table.add_column("Description")
    for key, data in catalog.items():
        table.add_row(key, data.get("category", ""), data.get("description", ""))
    console.print(table)


def _catalog_search_prompt(catalog: dict) -> str | None:
    """
    Scrollable + searchable catalog picker.
    Arrow keys to navigate, type to filter — Enter to confirm, Esc/Ctrl-C to cancel.
    Returns the catalog key selected, or None if cancelled.
    """
    if not catalog:
        return None

    choices = []
    for key, data in catalog.items():
        desc = data.get("description", "")
        category = data.get("category", "")
        choices.append(
            questionary.Choice(
                title=f"{key:<18} [{category}]  {desc}",
                value=key,
            )
        )

    return questionary.select(
        "Select package (↑↓ arrows, type to filter):",
        choices=choices,
        use_search_filter=True,
        use_jk_keys=False,
        use_indicator=True,
    ).ask()




def _add_subtasks(
    pkg_data: dict,
    plugin_type: str,
    pkg_version: str,
    codename: str,
    tasks: list,
    catalog: dict,
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

        resolved = resolve_subtask(subtask_def, plugin_type, pkg_version, codename, catalog)

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
# Custom task wizard
# ---------------------------------------------------------------------------

def _prompt_apt_dnf_task(task_name: str, plugin: str) -> dict:
    """Prompt for apt/dnf task fields (repos + packages)."""
    task: dict = {"name": task_name, "plugin": plugin}

    if questionary.confirm("Add a custom upstream repo?", default=False).ask():
        repo_name = questionary.text("Repo name (short key):").ask() or "custom"
        if plugin == "apt":
            repo_url = questionary.text(
                "Full apt source line  (e.g. deb [...] https://... /):"
            ).ask() or ""
        else:
            repo_url = questionary.text("Repo base URL:").ask() or ""

        gpg_key = questionary.text("GPG key URL (blank to skip):").ask() or ""
        repo: dict = {"name": repo_name, "url": repo_url}
        if gpg_key:
            repo["gpg_key"] = gpg_key
        task["repos"] = [repo]

    pkg_str = questionary.text("Packages (comma-separated):").ask() or ""
    task["packages"] = [p.strip() for p in pkg_str.split(",") if p.strip()]
    return task


def _prompt_pip_task(task_name: str) -> dict:
    task: dict = {"name": task_name, "plugin": "pip"}
    req_file = questionary.text(
        "requirements.txt path (blank to list packages inline):"
    ).ask()
    if req_file:
        task["requirements"] = req_file
    else:
        pkg_str = questionary.text("Python packages (comma-separated):").ask() or ""
        task["packages"] = [p.strip() for p in pkg_str.split(",") if p.strip()]
    task["python_version"] = questionary.text("Python version:", default="3.11").ask()
    return task


def _prompt_oci_task(task_name: str) -> dict:
    task: dict = {"name": task_name, "plugin": "oci_image"}
    rprint("[dim]Enter image references one per line. Leave blank and press Enter to stop.[/dim]")
    images = []
    while True:
        img = questionary.text("Image (blank to stop):").ask()
        if not img:
            break
        images.append(img)
    task["images"] = images
    return task


def _prompt_file_task(task_name: str) -> dict:
    task: dict = {"name": task_name, "plugin": "file"}
    files = []
    rprint("[dim]Add files/archives one at a time. Leave URL blank to stop.[/dim]")
    while True:
        url = questionary.text("File URL (blank to stop):").ask()
        if not url:
            break
        dest = questionary.text("Destination path:").ask() or "/tmp"
        extract = questionary.confirm("Extract archive?", default=False).ask()
        entry: dict = {"url": url, "dest": dest}
        if extract:
            entry["extract"] = True
            strip = questionary.text("Strip components (0 = none):", default="0").ask()
            entry["strip_components"] = int(strip or "0")
        else:
            entry["executable"] = questionary.confirm("Mark executable?", default=False).ask()
        files.append(entry)
    task["files"] = files
    return task


def _create_custom_task(default_plugin: str) -> dict | None:
    """
    Full interactive wizard to define a single custom task from scratch.
    Returns the task dict, or None if the user cancelled.
    """
    rprint("\n[bold]Custom Task[/bold]")
    task_name = questionary.text("Task name:").ask()
    if not task_name:
        return None

    plugin = questionary.select(
        "Plugin:",
        choices=["apt", "dnf", "pip", "oci_image", "file"],
        default=default_plugin,
    ).ask()

    if plugin in ("apt", "dnf"):
        return _prompt_apt_dnf_task(task_name, plugin)
    elif plugin == "pip":
        return _prompt_pip_task(task_name)
    elif plugin == "oci_image":
        return _prompt_oci_task(task_name)
    elif plugin == "file":
        return _prompt_file_task(task_name)
    return None


def _build_catalog_entry(task: dict, plugin: str) -> dict:
    """
    Convert a custom task dict into a catalog entry (subtask model).
    Plugin-neutral tasks (oci_image, file, pip) are stored under 'any'.
    """
    template_key = plugin if plugin in ("apt", "dnf") else "any"
    template = {k: v for k, v in task.items()}

    return {
        "subtasks": {
            "packages": {
                "label": task.get("name", "Custom task"),
                "required": True,
                "templates": {
                    template_key: template,
                },
            }
        }
    }


def _save_custom_task_to_catalog(task: dict, plugin: str) -> None:
    """Ask where to save the task and persist it to the chosen catalog file."""
    rprint("\n[bold]Save to Catalog[/bold]")

    entry_id = questionary.text(
        "Catalog entry ID (short key, used in 'syncit create' search):"
    ).ask()
    if not entry_id:
        rprint("[dim]Skipping catalog save.[/dim]")
        return

    description = questionary.text("Description:", default=task.get("name", "")).ask() or ""
    category = questionary.select(
        "Category:",
        choices=["infrastructure", "runtime", "database", "custom"],
        default="custom",
    ).ask()
    versions_str = questionary.text(
        "Supported versions (comma-separated, or 'latest'):", default="latest"
    ).ask() or "latest"
    versions = [v.strip() for v in versions_str.split(",") if v.strip()]

    entry = _build_catalog_entry(task, plugin)
    entry["description"] = description
    entry["category"] = category
    entry["versions"] = versions

    location = questionary.select(
        "Save location:",
        choices=[
            questionary.Choice(
                "User catalog  (~/.config/syncit/catalog.json)", "user"
            ),
            questionary.Choice(
                "Project catalog  (./syncit-catalog.json)", "project"
            ),
        ],
    ).ask()

    if location == "user":
        path = save_to_user_catalog(entry_id, entry)
    else:
        path = save_to_project_catalog(entry_id, entry)

    rprint(f"[green]Saved '{entry_id}' to:[/] {path}")
    rprint(f"[dim]Next run of 'syncit create' will show it in catalog search.[/dim]")


# ---------------------------------------------------------------------------
# Command
# ---------------------------------------------------------------------------

def create_cmd(
    manifest_file: Optional[Path] = typer.Argument(
        None,
        help="Path to an existing bundle.yaml to update. Omit to create a new one.",
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

    # ── Metadata prompts ──────────────────────────────────────────────────
    rprint("\n[bold]Bundle Metadata[/bold]")
    meta = existing.get("metadata", {})
    targets = existing.get("spec", {}).get("targets", {})

    bundle_name = questionary.text(
        "Bundle name:", default=meta.get("name", "")
    ).ask()
    if not bundle_name:
        raise typer.Exit()

    version = questionary.text("Version:", default=meta.get("version", "1.0.0")).ask()

    existing_distro_raw = targets.get("distro", "")
    mapping = {
        "ubuntu": "Ubuntu",
        "debian": "Debian",
        "rhel": "RHEL",
        "rocky": "Rocky",
        "almalinux": "AlmaLinux",
    }
    existing_distro = mapping.get(existing_distro_raw.lower(), "")
    detected_distro = _detect_distro()

    if existing_distro:
        default_distro = existing_distro
    elif detected_distro:
        default_distro = detected_distro
    else:
        default_distro = "Ubuntu"

    distro_choice = questionary.select(
        "Target distro:",
        choices=DISTRO_CHOICES,
        default=default_distro,
    ).ask()

    # ── Codename with auto-detect + hints ────────────────────────────────
    if distro_choice in APT_DISTROS:
        detected = _detect_codename()
        existing_codename = targets.get("codename", "")
        default_codename = existing_codename or detected or "noble"

        hints: list[str] = []
        if detected:
            hints.append(
                f"  [cyan]{detected}[/cyan]  [dim](recommended — detected on this machine)[/dim]"
            )
        if existing_codename and existing_codename != detected:
            hints.append(
                f"  [cyan]{existing_codename}[/cyan]  [dim](current in file)[/dim]"
            )
        if hints:
            rprint("[dim]Codename hints:[/dim]")
            for h in hints:
                rprint(h)

        codename = questionary.text("Codename:", default=default_codename).ask()
        plugin_type = "apt"
    else:
        codename = ""
        plugin_type = "dnf"

    existing_arch = targets.get("arch", "amd64")
    arch = questionary.select(
        "Architecture:",
        choices=["amd64", "arm64"],
        default=existing_arch if existing_arch in ("amd64", "arm64") else "amd64",
    ).ask()

    # Base installroot prompt
    has_base = any("base_installroot" in t for t in existing.get("spec", {}).get("tasks", []))
    enable_base = questionary.confirm(
        "Enable base_installroot for accurate dependency resolution? (apt/dnf tasks)",
        default=has_base,
    ).ask()
    
    base_root_path = ""
    if enable_base:
        existing_base = "/"
        for t in existing.get("spec", {}).get("tasks", []):
            if "base_installroot" in t:
                existing_base = t["base_installroot"]
                break
        base_root_path = questionary.text(
            "Base installroot path (e.g. / or /var/lib/minimal-root):",
            default=existing_base
        ).ask()
        
        if base_root_path:
            root_path = Path(base_root_path).expanduser().resolve()
            if not root_path.is_dir():
                choice = questionary.select(
                    f"Directory '{root_path}' does not exist. How would you like to initialize it? (requires sudo)",
                    choices=[
                        questionary.Choice("Empty Directory (Downloads ALL dependencies - Safer, Larger bundle)", "empty"),
                        questionary.Choice(f"Minimal OS Base (Uses {'debootstrap' if plugin_type == 'apt' else 'dnf @core'} - Optimized, Slower)", "baseos"),
                        questionary.Choice("Do not create", "none")
                    ]
                ).ask()

                if choice in ("empty", "baseos"):
                    from syncit.plugins.base import run_privileged
                    rprint(f"[cyan]Creating base_installroot at {root_path}...[/cyan]")
                    if plugin_type == "apt":
                        if choice == "empty":
                            run_privileged(["mkdir", "-p", f"{root_path}/var/lib/dpkg"])
                            run_privileged(["touch", f"{root_path}/var/lib/dpkg/status"])
                        else:
                            cn = codename or "noble"
                            rprint(f"[dim]Running: debootstrap {cn} {root_path}[/dim]")
                            res = run_privileged(["debootstrap", cn, str(root_path)])
                            if res.returncode != 0:
                                rprint(f"[red]debootstrap failed (is it installed?):[/] {res.stderr}")
                    else:
                        if choice == "empty":
                            run_privileged(["mkdir", "-p", str(root_path)])
                        else:
                            rprint(f"[dim]Running: dnf install --installroot {root_path} @core -y[/dim]")
                            res = run_privileged(["dnf", "install", "--installroot", str(root_path), "@core", "-y"])
                            if res.returncode != 0:
                                rprint(f"[red]dnf install failed:[/] {res.stderr}")
                    rprint(f"[green]Successfully initialized {root_path}[/green]")

    # Carry forward existing tasks; new tasks appended in the loop below
    tasks: list = list(existing.get("spec", {}).get("tasks", []))
    
    # If the user chose NOT to enable base_installroot, we should strip it out 
    # from any existing tasks (the "disable" part of the feature).
    if not enable_base:
        for t in tasks:
            if "base_installroot" in t:
                del t["base_installroot"]
    else:
        # If enabled, inject into all existing apt/dnf tasks
        if base_root_path:
            for t in tasks:
                if t.get("plugin") in ("apt", "dnf"):
                    t["base_installroot"] = base_root_path

    # ── Task loop ─────────────────────────────────────────────────────────
    while True:
        rprint("\n[bold]Add a task[/bold]")
        action = questionary.select(
            "What next?",
            choices=[
                "Search catalog",
                "Create custom task",
                "Add empty task",
                "Reload catalog",
                "Done",
            ],
        ).ask()

        # User pressed Ctrl+C on the action menu — treat as Done
        if action is None or action == "Done":
            break

        elif action == "Reload catalog":
            rprint("[cyan]Reloading catalog...[/cyan]")
            catalog = get_catalog()
            rprint(f"[green]Catalog reloaded[/green] — {len(catalog)} entries: {', '.join(sorted(catalog.keys()))}")
            continue

        elif action == "Add empty task":
            task_name = questionary.text("Task name:").ask()
            if task_name:
                t = {
                    "name": task_name,
                    "plugin": plugin_type,
                    "packages": ["<package_name>"],
                }
                if plugin_type in ("apt", "dnf") and base_root_path:
                    t["base_installroot"] = base_root_path
                tasks.append(t)
                rprint(f"[green]Task added:[/] {task_name}")

        elif action == "Create custom task":
            task = _create_custom_task(default_plugin=plugin_type)
            if task:
                if task.get("plugin") in ("apt", "dnf") and base_root_path:
                    task["base_installroot"] = base_root_path
                tasks.append(task)
                rprint(f"[green]Task added:[/] {task['name']}")
                if questionary.confirm(
                    "Save to catalog for future reuse?", default=False
                ).ask():
                    _save_custom_task_to_catalog(task, task.get("plugin", plugin_type))

        else:  # Search catalog
            if not catalog:
                rprint("[red]Catalog is empty. Try 'Reload catalog'.[/red]")
                continue

            pkg_key = _catalog_search_prompt(catalog)
            if not pkg_key:
                continue

            pkg_data = catalog[pkg_key]
            versions = pkg_data.get("versions", ["latest"])
            pkg_version = questionary.select("Version:", choices=versions).ask()
            if not pkg_version:  # cancelled
                continue

            _add_subtasks(pkg_data, plugin_type, pkg_version, codename, tasks, catalog)
            
            # Inject base_installroot into any newly added subtasks if applicable
            if base_root_path:
                for t in tasks:
                    if t.get("plugin") in ("apt", "dnf") and "base_installroot" not in t:
                        t["base_installroot"] = base_root_path

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
