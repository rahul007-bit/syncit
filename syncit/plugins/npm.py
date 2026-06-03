"""NPM plugin for caching project-level node_modules."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any

from syncit.plugins.base import (
    ApplyContext,
    DiffResult,
    OfflinePlugin,
    PackContext,
    PluginResult,
)
from syncit.plugins.registry import registry


class NpmPlugin(OfflinePlugin):
    name = "npm"

    def validate(self, task_spec: dict[str, Any]) -> list[str]:
        errors = []
        if not shutil.which("npm"):
            errors.append("[npm] Error: 'npm' command is missing from the system path.")

        projects = task_spec.get("projects", [])
        if not isinstance(projects, list):
            return ["[npm] 'projects' must be a list of tasks"]

        for idx, task in enumerate(projects):
            if "project_name" not in task:
                errors.append(f"[npm] task {idx} missing 'project_name'")
            if "project_dir" not in task:
                errors.append(f"[npm] task {idx} missing 'project_dir'")
        return errors

    def pack(self, task_spec: dict[str, Any], ctx: PackContext) -> PluginResult:
        errors = []
        artifacts: list[str] = []

        projects = task_spec.get("projects", [])
        for task in projects:
            import re
            proj_name = re.sub(r"[/\\]", "_", task["project_name"])
            proj_dir = Path(task["project_dir"]).expanduser().resolve()

            if not (proj_dir / "package.json").exists():
                errors.append(f"[npm] {proj_name}: package.json not found in {proj_dir}")
                continue

            # Run npm ci
            ci_cmd = ["npm", "ci"]
            if ctx.no_cache:
                if ctx.verbose:
                    print(f"[npm] {proj_name}: --no-cache provided, bypassing local cache...")
                # Unfortunately npm doesn't have a direct 'no-cache' flag for ci,
                # but we can force network with --prefer-online
                ci_cmd.append("--prefer-online")

            res = subprocess.run(ci_cmd, cwd=proj_dir, capture_output=True, text=True)
            if res.returncode != 0:
                errors.append(f"[npm] {proj_name}: npm ci failed.\n{res.stderr}")
                continue

            # Target bundle struct: bundle_dir/npm/<project_name>/
            target_dir = ctx.bundle_dir / "npm" / proj_name
            target_dir.mkdir(parents=True, exist_ok=True)

            nm_src = proj_dir / "node_modules"
            lock_src = proj_dir / "package-lock.json"

            if nm_src.exists():
                shutil.copytree(nm_src, target_dir / "node_modules", dirs_exist_ok=True)
                artifacts.append(f"npm/{proj_name}/node_modules")
            if lock_src.exists():
                shutil.copy2(lock_src, target_dir / "package-lock.json")
                artifacts.append(f"npm/{proj_name}/package-lock.json")

        return PluginResult(
            success=len(errors) == 0,
            message=f"Packed {len(artifacts)} npm artifacts"
            if not errors
            else f"Failed to pack npm ({len(errors)} errors)",
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

            bundled_nm = ctx.bundle_dir / "npm" / proj_name / "node_modules"
            if not bundled_nm.exists():
                # We skip missing, just log an error
                errors.append(f"[npm] {proj_name}: node_modules not found in bundle '{bundled_nm}'")
                continue

            proj_dir.mkdir(parents=True, exist_ok=True)

            # Copy node_modules to offline project dir
            dest_nm = proj_dir / "node_modules"
            shutil.copytree(bundled_nm, dest_nm, dirs_exist_ok=True)
            artifacts.append(str(dest_nm))

            # Write .npmrc offline constraints
            npmrc = proj_dir / ".npmrc"
            try:
                # Merge if exists to preserve user overrides
                content = ""
                if npmrc.exists():
                    content = npmrc.read_text()

                # Check line by line to prevent infinite duplication
                lines = content.splitlines()
                if "offline=true" not in lines:
                    lines.append("offline=true")
                if "prefer-offline=true" not in lines:
                    lines.append("prefer-offline=true")

                npmrc.write_text("\n".join(lines) + "\n")

                if str(npmrc) not in artifacts:
                    artifacts.append(str(npmrc))
            except Exception as e:
                errors.append(f"[npm] {proj_name}: failed to write .npmrc: {e}")

        return PluginResult(
            success=len(errors) == 0,
            message=f"Applied npm tasks with {len(errors)} errors",
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

    def render_apply_sh(self, task_spec: dict[str, Any], bundle_subdir: str) -> str:
        lines = [f'echo "[npm] Applying node_modules..."']
        import shlex
        for task in task_spec.get("projects", []):
            import re
            proj_name = re.sub(r"[/\\]", "_", task["project_name"])
            proj_dir = task["project_dir"]
            lines.append(f"cp -r $BUNDLE_DIR/{bundle_subdir}/{shlex.quote(proj_name)}/node_modules {shlex.quote(proj_dir)}/")
            lines.append(f"echo 'offline=true' >> {shlex.quote(proj_dir)}/.npmrc")
            lines.append(f"echo 'prefer-offline=true' >> {shlex.quote(proj_dir)}/.npmrc")
        return "\\n".join(lines) + "\\n"


registry.register(NpmPlugin())
