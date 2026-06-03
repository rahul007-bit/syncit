"""DNF plugin for offline RPM caching and repo creation."""

from __future__ import annotations

import json
import shutil
import subprocess
import urllib.request
from pathlib import Path
from typing import Any

from rich.console import Console

from syncit.plugins.base import (
    ApplyContext,
    DiffResult,
    OfflinePlugin,
    PackContext,
    PluginResult,
)
from syncit.plugins.registry import registry

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

        # Validate optional repos
        repos = task_spec.get("repos", [])
        if repos:
            if not isinstance(repos, list):
                errors.append("[dnf] 'repos' must be a list")
            else:
                has_download_tool = (
                    shutil.which("curl") is not None or shutil.which("wget") is not None
                )
                for i, repo in enumerate(repos):
                    if not isinstance(repo, dict):
                        errors.append(
                            f"[dnf] repos[{i}] must be an object (with 'name' and 'baseurl')"
                        )
                        continue
                    if "name" not in repo or not isinstance(repo.get("name"), str):
                        errors.append(f"[dnf] repos[{i}] missing required string field: 'name'")
                    if "baseurl" not in repo or not isinstance(repo.get("baseurl"), str):
                        errors.append(f"[dnf] repos[{i}] missing required string field: 'baseurl'")
                    if repo.get("gpgkey") and not has_download_tool:
                        errors.append(
                            f"[dnf] repos[{i}]['{repo['name']}'] has 'gpgkey' but neither 'curl' nor 'wget' is installed"
                        )

        return errors

    def pack(self, task_spec: dict[str, Any], ctx: PackContext) -> PluginResult:
        packages = task_spec.get("packages", [])
        repos: list[dict[str, Any]] = task_spec.get("repos", [])
        if not packages:
            return PluginResult(True, "No dnf tasks", [], [])

        errors = []
        artifacts: list[str] = []

        rpm_dir = ctx.bundle_dir / "dnf" / "rpms"
        dnf_dir = ctx.bundle_dir / "dnf"
        keys_dir = dnf_dir / "keys"
        rpm_dir.mkdir(parents=True, exist_ok=True)

        # Cache directory
        cache_dir = Path("~/.cache/syncit/dnf").expanduser()
        cache_dir.mkdir(parents=True, exist_ok=True)

        # ── Phase 0: Process upstream repos ─────────────────────────────────
        extra_repo_opts: list[str] = []
        if repos:
            keys_dir.mkdir(exist_ok=True)
            if ctx.verbose:
                print(f"[dnf] Processing {len(repos)} upstream repo(s)...")

            for repo in repos:
                name = repo.get("name", "repo")
                baseurl = repo.get("baseurl", "")

                # Download GPG key into bundle for audit trail
                gpgkey_url = repo.get("gpgkey")
                if gpgkey_url:
                    try:
                        key_dest = keys_dir / f"{name}.key"
                        if not key_dest.exists() or ctx.no_cache:
                            if ctx.verbose:
                                print(f"    [dnf] Downloading GPG key for '{name}'...")
                            urllib.request.urlretrieve(gpgkey_url, str(key_dest))
                    except Exception as exc:
                        errors.append(f"[dnf] Failed to download GPG key for repo '{name}': {exc}")

                # Add repo via --repofrompath (no system mutation)
                extra_repo_opts.extend(["--repofrompath", f"{name},{baseurl}"])
                if ctx.verbose:
                    print(f"    [dnf] Added repo '{name}' ({baseurl})")

        # ── Phase 1: Download packages ──────────────────────────────────────
        dl_cmd = (
            ["dnf", "download", "--resolve"]
            + extra_repo_opts
            + ["--destdir", str(cache_dir)]
            + packages
        )

        if ctx.no_cache:
            if ctx.verbose:
                print(f"[dnf] --no-cache: clearing local cache for {packages}...")
            for pkg in packages:
                for f in cache_dir.glob(f"{pkg}-*"):
                    try:
                        f.unlink()
                    except OSError:
                        pass

        res = subprocess.run(dl_cmd, capture_output=True, text=True)
        if res.returncode != 0:
            errors.append(f"[dnf] download failed: {res.stderr}")
            return PluginResult(False, "Failed to download RPMs", artifacts, errors)

        # 2. Copy artifacts from cache to bundle
        for f in cache_dir.iterdir():
            if f.is_file() and f.suffix == ".rpm":
                shutil.copy2(f, rpm_dir / f.name)

        res2 = subprocess.run(
            ["createrepo_c", str(rpm_dir)],
            capture_output=True,
            text=True,
        )
        if res2.returncode != 0:
            errors.append(f"[dnf] createrepo_c failed: {res2.stderr}")
            return PluginResult(False, "Failed to create repo", artifacts, errors)

        # ── Phase 3: Persist repo metadata for audit / diff ─────────────────
        if repos:
            meta = []
            for repo in repos:
                entry: dict[str, Any] = {
                    "name": repo["name"],
                    "baseurl": repo["baseurl"],
                }
                if repo.get("gpgkey"):
                    entry["gpgkey_url"] = repo["gpgkey"]
                if repo.get("gpgcheck"):
                    entry["gpgcheck"] = repo["gpgcheck"]
                meta.append(entry)
            (dnf_dir / "repos.json").write_text(json.dumps(meta, indent=2))

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
        if old_spec is None:
            new_pkgs = new_spec.get("packages", [])
            new_repos = new_spec.get("repos", [])
            added = [f"[repo] {r['name']} ({r.get('baseurl', '')})" for r in new_repos] + new_pkgs
            return DiffResult(
                plugin_name=self.name, added=added, removed=[], updated=[], unchanged=[]
            )

        # Compare repos
        old_repos = {(r.get("name", ""), r.get("baseurl", "")) for r in old_spec.get("repos", [])}
        new_repos = {(r.get("name", ""), r.get("baseurl", "")) for r in new_spec.get("repos", [])}

        added_repos = sorted(new_repos - old_repos)
        removed_repos = sorted(old_repos - new_repos)

        added = [f"[repo] {n} ({u})" for n, u in added_repos]
        removed = [f"[repo] {n} ({u})" for n, u in removed_repos]

        # Compare packages
        old_set = set((old_spec or {}).get("packages", []))
        new_set = set(new_spec.get("packages", []))

        added += sorted(new_set - old_set)
        removed += sorted(old_set - new_set)

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
cat > /etc/yum.repos.d/syncit.repo << 'EOF'
[syncit]
name=Syncit Local Repo
baseurl=file:///srv/offline/dnf/rpms
enabled=1
gpgcheck=0
EOF
dnf install -y {packages}
"""


registry.register(DnfPlugin())
