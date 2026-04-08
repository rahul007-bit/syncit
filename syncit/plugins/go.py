"""Go plugin for caching GOMODCACHE offline."""

from __future__ import annotations

import os
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


class GoPlugin(OfflinePlugin):
    name = "go"

    def validate(self, task_spec: dict[str, Any]) -> list[str]:
        errors = []
        if not shutil.which("go"):
            errors.append("[go] Error: 'go' command is missing from the system path.")

        projects = task_spec.get("projects", [])
        if not isinstance(projects, list):
            return ["[go] 'projects' must be a list of tasks"]

        for idx, task in enumerate(projects):
            if "project_name" not in task:
                errors.append(f"[go] task {idx} missing 'project_name'")
            if "project_dir" not in task:
                errors.append(f"[go] task {idx} missing 'project_dir'")
        return errors

    def pack(self, task_spec: dict[str, Any], ctx: PackContext) -> PluginResult:
        errors = []
        artifacts: list[str] = []

        target_cache = ctx.bundle_dir / "go" / "modcache"
        target_cache.mkdir(parents=True, exist_ok=True)

        env = os.environ.copy()
        env["GOMODCACHE"] = str(target_cache)

        projects = task_spec.get("projects", [])
        for task in projects:
            proj_name = task["project_name"]
            proj_dir = Path(task["project_dir"]).expanduser().resolve()

            if not (proj_dir / "go.mod").exists():
                errors.append(f"[go] {proj_name}: go.mod not found in {proj_dir}")
                continue

            res = subprocess.run(
                ["go", "mod", "download", "./..."],
                cwd=proj_dir,
                env=env,
                capture_output=True,
                text=True,
            )
            if res.returncode != 0:
                errors.append(f"[go] {proj_name}: go mod download failed.\n{res.stderr}")
                continue

        if target_cache.exists() and any(target_cache.iterdir()):
            artifacts.append("go/modcache")

        return PluginResult(
            success=len(errors) == 0,
            message=f"Packed go modcache for {len(task_spec)} module(s)"
            if not errors
            else f"Failed to pack go ({len(errors)} errors)",
            artifacts=artifacts,
            errors=errors,
        )

    def apply(self, task_spec: dict[str, Any], ctx: ApplyContext) -> PluginResult:
        errors = []
        artifacts: list[str] = []

        modcache_src = ctx.bundle_dir / "go" / "modcache"
        projects = task_spec.get("projects", [])
        if not modcache_src.exists():
            if projects:
                errors.append("[go] modcache not found in bundle")
            return PluginResult(
                success=len(errors) == 0,
                message="Applied go",
                artifacts=artifacts,
                errors=errors,
            )

        dest_modcache = Path("/opt/offline/go/modcache")
        script_path = Path("/etc/profile.d/offline-go.sh")

        try:
            dest_modcache.mkdir(parents=True, exist_ok=True)
            shutil.copytree(modcache_src, dest_modcache, dirs_exist_ok=True)
            artifacts.append(str(dest_modcache))

            script_content = (
                f"export GOMODCACHE={dest_modcache}\nexport GOPROXY=off\nexport GONOSUMCHECK=*\n"
            )
            script_path.parent.mkdir(parents=True, exist_ok=True)
            script_path.write_text(script_content)
            artifacts.append(str(script_path))

        except PermissionError as e:
            errors.append(
                f"[go] Permission denied writing to system paths: {e}. Please run as root."
            )
        except Exception as e:
            errors.append(f"[go] Failed to apply go cache: {e}")

        return PluginResult(
            success=len(errors) == 0,
            message="Applied go modcache mapping to /opt/offline",
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
        return f"""
echo "[go] Applying global go modcache..."
mkdir -p /opt/offline/go/modcache
cp -r $BUNDLE_DIR/{bundle_subdir}/modcache/* /opt/offline/go/modcache/ 2>/dev/null || true
echo "export GOMODCACHE=/opt/offline/go/modcache" > /etc/profile.d/offline-go.sh
echo "export GOPROXY=off" >> /etc/profile.d/offline-go.sh
echo "export GONOSUMCHECK=*" >> /etc/profile.d/offline-go.sh
"""


registry.register(GoPlugin())
