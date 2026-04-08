"""Expand roles in a manifest — merges role tasks into the task list before commands run."""

from __future__ import annotations

from pathlib import Path

from syncit.manifest.schema import BundleManifest
from syncit.roles.loader import load_role


def expand_roles(manifest: BundleManifest, manifest_dir: Path) -> BundleManifest:
    """Expand role references in a manifest into concrete tasks.

    Role tasks are prepended before inline tasks. Role paths are resolved
    relative to the manifest directory.

    Duplicate task names (same plugin + name) across roles or inline tasks
    raise a ValueError.

    Args:
        manifest: The loaded BundleManifest (may have spec.roles entries).
        manifest_dir: Directory of the bundle.yaml file (for relative role paths).

    Returns:
        A new BundleManifest with role tasks merged into spec.tasks.

    Raises:
        FileNotFoundError: if a role path does not exist.
        ValueError: if a duplicate task name is found.
    """
    role_refs: list[dict] = manifest.spec.roles  # type: ignore[attr-defined]
    if not role_refs:
        return manifest

    merged_tasks: list[dict] = []
    seen: dict[str, str] = {}  # "plugin:name" -> "source (role or inline)"

    # --- Expand role tasks first ---
    for ref in role_refs:
        raw_path = ref.get("path", "")
        role_path = (manifest_dir / raw_path).resolve()

        role = load_role(role_path)  # raises FileNotFoundError if missing

        for task in role.tasks:
            key = f"{task.plugin}:{task.name}"
            if key in seen:
                raise ValueError(
                    f"Duplicate task '{task.name}' (plugin: {task.plugin}) — "
                    f"already defined in {seen[key]}. "
                    f"Each plugin+name combination must be unique across roles and inline tasks."
                )
            seen[key] = f"role '{role.name}' at '{role_path}'"

            # Convert back to the raw task dict format expected by manifest.spec.tasks
            task_dict: dict = {"plugin": task.plugin, "name": task.name}
            task_dict.update(task.spec)
            merged_tasks.append(task_dict)

    # --- Then inline tasks ---
    for task_dict in manifest.spec.tasks:
        plugin = task_dict.get("plugin", "")
        name = task_dict.get("name", "")
        key = f"{plugin}:{name}"
        if key in seen:
            raise ValueError(
                f"Duplicate task '{name}' (plugin: {plugin}) — "
                f"already defined in {seen[key]}. "
                f"Each plugin+name combination must be unique across roles and inline tasks."
            )
        seen[key] = "inline tasks"
        merged_tasks.append(task_dict)

    # Rebuild manifest with merged task list
    new_data = manifest.model_dump()
    new_data["spec"]["tasks"] = merged_tasks
    return BundleManifest(**new_data)
