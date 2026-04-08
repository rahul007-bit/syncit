"""DNF plugin for offline RPM caching and repo creation."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any

from rich.console import Console

from offlinectl.plugins.base import (
    ApplyContext,
    DiffResult,
    OfflinePlugin,
    PackContext,
    PluginResult,
)
from offlinectl.plugins.registry import registry

err_console = Console(stderr=True)


class DnfPlugin(OfflinePlugin):
    name = "dnf"

    def validate(self, task_spec: dict[str, Any]) -> list[str]:
        errors = []
        if not shutil.which("dnf"):
            errors.append("[dnf] Error: 'dnf' command is missing.")
        if not shutil.which("createrepo_c"):
            errors.append("[dnf] Error: 'createrepo_c' command is missing.")

        packages = task_spec.get("packages", [])
        if not isinstance(packages, list):
            return ["[dnf] 'packages' must be a list of package strings"]

        # Preflight OS Release check
        os_release = Path("/etc/os-release")
        if os_release.exists():
            content = os_release.read_text().lower()
            if not any(
                f'id="{os_id}"' in content or f"id={os_id}\n" in content
                for os_id in ["rhel", "rocky", "almalinux", "centos"]
            ):
                err_console.print(
                    "[yellow][Warning] Host OS may not be RHEL/Rocky/Alma/CentOS. DNF operations may be degraded.[/yellow]"
                )
        else:
            err_console.print("[yellow][Warning] /etc/os-release not found.[/yellow]")

        for idx, pkg in enumerate(packages):
            if not isinstance(pkg, str):
                errors.append(f"[dnf] task {idx} must be a package string")

        return errors

    def pack(self, task_spec: dict[str, Any], ctx: PackContext) -> PluginResult:
        packages = task_spec.get("packages", [])
        if not packages:
            return PluginResult(True, "No dnf tasks", [], [])

        errors = []
        artifacts: list[str] = []

        rpm_dir = ctx.bundle_dir / "dnf" / "rpms"
        rpm_dir.mkdir(parents=True, exist_ok=True)

        res = subprocess.run(
            ["dnf", "download", "--resolve", "--destdir", str(rpm_dir)] + packages,
            capture_output=True,
            text=True,
        )
        if res.returncode != 0:
            errors.append(f"[dnf] download failed: {res.stderr}")
            return PluginResult(False, "Failed to download RPMs", artifacts, errors)

        res2 = subprocess.run(
            ["createrepo_c", str(rpm_dir)],
            capture_output=True,
            text=True,
        )
        if res2.returncode != 0:
            errors.append(f"[dnf] createrepo_c failed: {res2.stderr}")
            return PluginResult(False, "Failed to create repo", artifacts, errors)

        artifacts.append("dnf/rpms")

        return PluginResult(
            success=True,
            message=f"Packed {len(packages)} DNF package rules",
            artifacts=artifacts,
            errors=errors,
        )

    def apply(self, task_spec: dict[str, Any], ctx: ApplyContext) -> PluginResult:
        packages = task_spec.get("packages", [])
        if not packages:
            return PluginResult(True, "No dnf tasks", [], [])

        errors = []
        artifacts: list[str] = []

        bundled_rpms = ctx.bundle_dir / "dnf" / "rpms"
        if not bundled_rpms.exists():
            return PluginResult(False, "No bundled RPMs found", [], ["[dnf] rpms missing"])

        dest_rpm = Path("/srv/offline/dnf/rpms")
        repo_path = Path("/etc/yum.repos.d/offline.repo")

        try:
            dest_rpm.mkdir(parents=True, exist_ok=True)
            shutil.copytree(bundled_rpms, dest_rpm, dirs_exist_ok=True)
            artifacts.append(str(dest_rpm))

            res = subprocess.run(
                ["createrepo_c", str(dest_rpm)],
                capture_output=True,
                text=True,
            )
            if res.returncode != 0:
                raise Exception(f"createrepo_c failed: {res.stderr}")

            repo_content = "[offline]\nname=Offline Bundle\nbaseurl=file:///srv/offline/dnf/rpms\nenabled=1\ngpgcheck=0\n"
            repo_path.parent.mkdir(parents=True, exist_ok=True)
            repo_path.write_text(repo_content)
            artifacts.append(str(repo_path))

            res_inst = subprocess.run(
                ["dnf", "install", "-y"] + packages,
                capture_output=True,
                text=True,
            )
            if res_inst.returncode != 0:
                raise Exception(f"dnf install failed: {res_inst.stderr}")

        except Exception as e:
            errors.append(f"[dnf] Failed during apply: {e}")

        return PluginResult(
            success=len(errors) == 0,
            message="Applied offline yum repo and installed packages",
            artifacts=artifacts,
            errors=errors,
        )

    def diff(self, old_spec: dict[str, Any] | None, new_spec: dict[str, Any]) -> DiffResult:
        old_set = set((old_spec or {}).get("packages", []))
        new_set = set(new_spec.get("packages", []))

        added = list(new_set - old_set)
        removed = list(old_set - new_set)

        return DiffResult(
            plugin_name=self.name,
            added=added,
            removed=removed,
            updated=[],
            unchanged=[],
        )

    def render_apply_sh(self, task_spec: dict[str, Any], bundle_subdir: str) -> str:
        packages = " ".join(task_spec.get("packages", []))
        return f"""
echo "[dnf] Configuring local repository..."
mkdir -p /srv/offline/dnf/rpms
cp -r $BUNDLE_DIR/{bundle_subdir}/rpms/* /srv/offline/dnf/rpms/
createrepo_c /srv/offline/dnf/rpms
cat > /etc/yum.repos.d/offlinectl.repo << 'EOF'
[offlinectl]
name=Offlinectl Local Repo
baseurl=file:///srv/offline/dnf/rpms
enabled=1
gpgcheck=0
EOF
dnf install -y {packages}
"""


registry.register(DnfPlugin())
