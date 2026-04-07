"""Cargo plugin for vendoring Rust dependencies offline."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any

from offlinectl.plugins.base import (
    ApplyContext,
    DiffResult,
    OfflinePlugin,
    PackContext,
    PluginResult,
)
from offlinectl.plugins.registry import registry


class CargoPlugin(OfflinePlugin):
    name = "cargo"

    def validate(self, task_spec: dict[str, Any]) -> list[str]:
        errors = []
        if not shutil.which("cargo"):
            errors.append("[cargo] Error: 'cargo' command is missing from the system path.")

        projects = task_spec.get("projects", [])
        if not isinstance(projects, list):
            return ["[cargo] 'projects' must be a list of tasks"]

        for idx, task in enumerate(projects):
            if "project_name" not in task:
                errors.append(f"[cargo] task {idx} missing 'project_name'")
            if "project_dir" not in task:
                errors.append(f"[cargo] task {idx} missing 'project_dir'")
        return errors

    def pack(self, task_spec: dict[str, Any], ctx: PackContext) -> PluginResult:
        errors = []
        artifacts: list[str] = []

        projects = task_spec.get("projects", [])
        for task in projects:
            proj_name = task["project_name"]
            proj_dir = Path(task["project_dir"]).expanduser().resolve()

            if not (proj_dir / "Cargo.toml").exists():
                errors.append(f"[cargo] {proj_name}: Cargo.toml not found in {proj_dir}")
                continue

            target_dir = ctx.bundle_dir / "cargo" / proj_name
            target_dir.mkdir(parents=True, exist_ok=True)
            vendor_dir = target_dir / "vendor"

            res = subprocess.run(
                ["cargo", "vendor", str(vendor_dir)],
                cwd=proj_dir,
                capture_output=True,
                text=True,
            )
            if res.returncode != 0:
                errors.append(f"[cargo] {proj_name}: cargo vendor failed.\n{res.stderr}")
                continue

            # Save the printed stdout config snippet to be applied later
            config_snippet = target_dir / "config.toml.snippet"
            config_snippet.write_text(res.stdout)

            artifacts.append(f"cargo/{proj_name}/vendor")
            artifacts.append(f"cargo/{proj_name}/config.toml.snippet")

        return PluginResult(
            success=len(errors) == 0,
            message=f"Packed {len(artifacts)} cargo artifacts"
            if not errors
            else f"Failed to pack cargo ({len(errors)} errors)",
            artifacts=artifacts,
            errors=errors,
        )

    def apply(self, task_spec: dict[str, Any], ctx: ApplyContext) -> PluginResult:
        errors = []
        artifacts: list[str] = []

        projects = task_spec.get("projects", [])
        for task in projects:
            proj_name = task["project_name"]
            proj_dir = Path(task["project_dir"]).expanduser().resolve()

            bundled_vendor = ctx.bundle_dir / "cargo" / proj_name / "vendor"
            bundled_snippet = ctx.bundle_dir / "cargo" / proj_name / "config.toml.snippet"

            if not bundled_vendor.exists():
                errors.append(f"[cargo] {proj_name}: vendor fallback missing in bundle")
                continue

            proj_dir.mkdir(parents=True, exist_ok=True)
            dest_vendor = proj_dir / "vendor"
            shutil.copytree(bundled_vendor, dest_vendor, dirs_exist_ok=True)
            artifacts.append(str(dest_vendor))

            cargo_config_dir = proj_dir / ".cargo"
            cargo_config_dir.mkdir(parents=True, exist_ok=True)
            cargo_config = cargo_config_dir / "config.toml"

            default_snippet = """
[source.crates-io]
replace-with = "vendored-sources"

[source.vendored-sources]
directory = "vendor"
"""
            append_text = (
                bundled_snippet.read_text() if bundled_snippet.exists() else default_snippet
            )

            try:
                content = cargo_config.read_text() if cargo_config.exists() else ""

                # Check line by line to prevent infinite duplication
                if "replace-with" not in content and "[source.crates-io]" not in content:
                    with open(cargo_config, "a") as f:
                        f.write("\n" + append_text + "\n")

                if str(cargo_config) not in artifacts:
                    artifacts.append(str(cargo_config))
            except Exception as e:
                errors.append(f"[cargo] {proj_name}: failed to write config.toml: {e}")

        return PluginResult(
            success=len(errors) == 0,
            message=f"Applied cargo tasks with {len(errors)} errors",
            artifacts=artifacts,
            errors=errors,
        )

    def diff(self, old_spec: dict[str, Any] | None, new_spec: dict[str, Any]) -> DiffResult:
        old_list = (old_spec or {}).get("projects", [])
        new_list = new_spec.get("projects", [])

        old_keys = {f"{t.get('project_name')}@{t.get('project_dir')}" for t in old_list}
        new_keys = {f"{t.get('project_name')}@{t.get('project_dir')}" for t in new_list}

        added = list(new_keys - old_keys)
        removed = list(old_keys - new_keys)

        return DiffResult(
            plugin_name=self.name,
            added=added,
            removed=removed,
            updated=[],
            unchanged=[],
        )


registry.register(CargoPlugin())
