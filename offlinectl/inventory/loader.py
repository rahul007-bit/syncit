from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import ValidationError

from offlinectl.inventory.schema import Host, Inventory


def load_inventory(path: Path) -> Inventory:
    """Load and validate an inventory YAML file."""
    if not path.is_file():
        raise FileNotFoundError(f"Inventory file not found: {path}")

    try:
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f)
            if data is None:
                data = {}
    except yaml.YAMLError as e:
        raise ValueError(f"Invalid YAML syntax in inventory {path}: {e}") from e

    try:
        inventory = Inventory.model_validate(data)
    except ValidationError as e:
        raise ValueError(f"Inventory validation failed for {path}:\n{e}") from e

    # Extra validation: ensure all hosts in groups exist
    for group_name, members in inventory.groups.items():
        for member in members:
            if member not in inventory.hosts:
                raise ValueError(f"Group '{group_name}' defines undefined host '{member}'.")

    return inventory


def resolve_targets(inventory: Inventory, target_name: str) -> list[tuple[str, Host]]:
    """
    Resolve a target string into a list of (host_name, Host) tuples.
    Throws ValueError if target_name is neither a host nor a group.
    """
    if target_name in inventory.groups:
        return [(host_id, inventory.hosts[host_id]) for host_id in inventory.groups[target_name]]

    if target_name in inventory.hosts:
        return [(target_name, inventory.hosts[target_name])]

    raise ValueError(f"Unknown target '{target_name}' not found in inventory hosts or groups.")
