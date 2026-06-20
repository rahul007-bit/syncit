import typer
import yaml
import questionary
from pathlib import Path
from rich import print as rprint
from rich.console import Console

from syncit.registry import get_catalog, resolve_template
from syncit.commands.pack import run_pack
from syncit.commands.up import up_cmd

console = Console()

def get_package_choices(catalog: dict) -> list[questionary.Choice]:
    choices = []
    for key, data in catalog.items():
        desc = data.get("description", "")
        choices.append(questionary.Choice(title=f"{key:<15} ({desc})", value=key))
    return choices

def create_cmd() -> None:
    catalog = get_catalog()
    if not catalog:
        rprint("[yellow]Warning: Could not fetch catalog. Proceeding anyway.[/yellow]")
        catalog = {}

    rprint("\n[bold]Bundle Metadata[/bold]")
    bundle_name = questionary.text("Bundle name:").ask()
    if not bundle_name:
        raise typer.Exit()
        
    version = questionary.text("Version:", default="1.0.0").ask()
    
    distro_choice = questionary.select(
        "Target distro:",
        choices=["Ubuntu", "Debian", "RHEL", "Rocky", "AlmaLinux"]
    ).ask()
    
    if distro_choice in ["Ubuntu", "Debian"]:
        codename = questionary.text("Codename (e.g., noble, jammy, bookworm):", default="noble").ask()
        plugin_type = "apt"
    else:
        codename = ""
        plugin_type = "dnf"
        
    arch = questionary.select("Architecture:", choices=["amd64", "arm64"]).ask()
    
    tasks = []
    
    while True:
        rprint("\n[bold]Add a task[/bold]")
        action = questionary.select(
            "What next?",
            choices=["Search catalog", "Add empty task", "Done"]
        ).ask()
        
        if action == "Done":
            break
            
        if action == "Add empty task":
            task_name = questionary.text("Task name:").ask()
            tasks.append({
                "name": task_name,
                "plugin": plugin_type,
                "packages": ["<package_name>"]
            })
            rprint(f"[green]Task added:[/] {task_name}")
            continue
            
        # Search catalog
        choices = get_package_choices(catalog)
        if not choices:
            rprint("[red]Catalog is empty.[/red]")
            continue
            
        pkg_key = questionary.select(
            "Search packages:",
            choices=choices,
            use_indicator=True,
            use_shortcuts=True
        ).ask()
        
        if not pkg_key:
            continue
            
        pkg_data = catalog[pkg_key]
        versions = pkg_data.get("versions", ["latest"])
        
        pkg_version = questionary.select(
            "Version:",
            choices=versions
        ).ask()
        
        # Resolve package template
        templates = pkg_data.get("templates", {})
        if plugin_type not in templates:
            rprint(f"[red]Error: {pkg_key} does not have a {plugin_type} template.[/red]")
            continue
            
        resolved_task = resolve_template(templates[plugin_type], pkg_version, codename)
        tasks.append(resolved_task)
        rprint(f"[green]Task added:[/] {resolved_task['name']}")
        
        # Check for images
        images = pkg_data.get("images", [])
        if images:
            include_images = questionary.confirm("Include associated OCI images?").ask()
            if include_images:
                for img_template in images:
                    resolved_img = resolve_template(img_template, pkg_version, codename)
                    tasks.append(resolved_img)
                    rprint(f"[green]Task added:[/] {resolved_img['name']}")

    # Build the final manifest
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
                "arch": arch
            },
            "tasks": tasks
        }
    }
    if codename:
        manifest["spec"]["targets"]["codename"] = codename

    rprint("\n")
    save_path = questionary.text("Save to:", default="bundle.yaml").ask()
    if not save_path:
        raise typer.Exit()
        
    save_file = Path(save_path)
    with open(save_file, "w") as f:
        yaml.dump(manifest, f, sort_keys=False)
        
    rprint(f"[green]Saved {save_file}[/green]")
    
    # Prompt to run
    run_choice = questionary.select(
        "Run now?",
        choices=[
            questionary.Choice("Pack bundle locally", "pack"),
            questionary.Choice("Pack and apply remotely (syncit up)", "up"),
            questionary.Choice("Not yet", "none")
        ]
    ).ask()
    
    if run_choice == "none":
        return
        
    rprint(f"\n[cyan]Starting syncit {run_choice}...[/cyan]")
    output_dir = Path("./bundles")
    
    if run_choice == "pack":
        try:
            run_pack(manifest=save_file, output=output_dir, dry_run=False)
        except Exception as e:
            rprint(f"[red]Pack failed:[/] {e}")
            raise typer.Exit(1)
            
    elif run_choice == "up":
        inventory_path = questionary.text("Inventory file path:", default="inventory.yaml").ask()
        if not inventory_path or not Path(inventory_path).exists():
            rprint("[red]Valid inventory file is required.[/red]")
            raise typer.Exit(1)
            
        # Parse the inventory quickly to present host options
        # A simple fallback if no yaml parser:
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
            up_cmd(
                manifest=save_file,
                output=output_dir,
                inventory=Path(inventory_path),
                target=target_host,
                dry_run=False
            )
        except Exception as e:
            rprint(f"[red]Up failed:[/] {e}")
            raise typer.Exit(1)
