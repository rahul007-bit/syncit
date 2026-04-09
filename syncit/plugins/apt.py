"""apt plugin — downloads .deb packages and sets up a local apt repository."""

from __future__ import annotations

import re
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


def _run(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, **kwargs)


def _detect_codename() -> str:
    """Return the Ubuntu/Debian codename of the running system."""
    try:
        result = _run(["lsb_release", "-cs"])
        if result.returncode == 0:
            return result.stdout.strip()
    except FileNotFoundError:
        pass
    # Fallback: parse /etc/os-release
    try:
        data = Path("/etc/os-release").read_text()
        for line in data.splitlines():
            if line.startswith("VERSION_CODENAME="):
                return line.split("=", 1)[1].strip().strip('"')
    except OSError:
        pass
    return "unknown"


class AptPlugin(OfflinePlugin):
    name = "apt"

    def validate(self, task_spec: dict[str, Any]) -> list[str]:
        errors: list[str] = []
        if not shutil.which("dpkg-scanpackages"):
            errors.append(
                "[apt] Error: 'dpkg-scanpackages' not found — install dpkg-dev (sudo apt-get install dpkg-dev)"
            )
        packages = task_spec.get("packages")
        if not packages or not isinstance(packages, list):
            errors.append("[apt] 'packages' must be a non-empty list")
            return errors
        for pkg in packages:
            if not isinstance(pkg, str) or not pkg.strip():
                errors.append(f"[apt] Invalid package entry: {pkg!r}")
        return errors

    def pack(self, task_spec: dict[str, Any], ctx: PackContext) -> PluginResult:
        packages: list[str] = task_spec.get("packages", [])
        deb_dir = ctx.bundle_dir / "apt" / "debs"
        deb_dir.mkdir(parents=True, exist_ok=True)

        if ctx.dry_run:
            return PluginResult(
                success=True,
                message=f"[dry-run] Would download {len(packages)} package(s) + dependencies",
                artifacts=[],
                errors=[],
            )

        errors: list[str] = []
        all_pkgs: set[str] = set()

        # 1. Resolve recursive dependencies for each package
        if ctx.verbose:
            from rich import print as rprint

            rprint(f"[cyan]→[/] Resolving dependencies for: {' '.join(packages)}...")

        for pkg in packages:
            dep_cmd = [
                "apt-cache",
                "depends",
                "--recurse",
                "--no-recommends",
                "--no-suggests",
                "--no-conflicts",
                "--no-breaks",
                "--no-replaces",
                "--no-enhances",
                pkg,
            ]
            dep_res = _run(dep_cmd)
            if dep_res.returncode != 0:
                errors.append(
                    f"[apt] apt-cache depends failed for '{pkg}': {dep_res.stderr.strip()}"
                )
                continue

            all_pkgs.add(pkg)
            for line in dep_res.stdout.splitlines():
                line = line.strip()
                if ">" in line or "<" in line:
                    continue
                if line.startswith("|"):
                    continue
                if ":any" in line or ":native" in line:
                    continue
                line = line.removeprefix("Depends:").removeprefix("PreDepends:").strip()
                if not line:
                    continue
                name = line.split()[0]
                if re.match(r"^[a-z0-9][a-z0-9.+\-]+$", name):
                    all_pkgs.add(name)

        if not all_pkgs and errors:
            return PluginResult(
                success=False, message="Dependency resolution failed", artifacts=[], errors=errors
            )

        sorted_pkgs = sorted(list(all_pkgs))
        total_dl = len(sorted_pkgs)
        if ctx.verbose:
            rprint(f"[cyan]→[/] Resolved {total_dl} packages (including transitive deps)")

        # 2. Download all resolved packages with caching
        cache_dir = Path("~/.cache/syncit/apt").expanduser()
        cache_dir.mkdir(parents=True, exist_ok=True)

        for i, p in enumerate(sorted_pkgs, start=1):
            if ctx.verbose:
                rprint(f"[cyan]→[/] Processing: {p} ({i}/{total_dl})...")

            # Resolve exact filename for cache check
            # Format: name_version_arch.deb (with epoch : replaced by %3a)
            pkg_info = _run(["apt-cache", "show", p])
            if pkg_info.returncode != 0:
                errors.append(f"[apt] apt-cache show failed for '{p}'")
                continue

            ver, arch = "", ""
            for line in pkg_info.stdout.splitlines():
                if line.startswith("Version: "):
                    ver = line.split(": ", 1)[1]
                if line.startswith("Architecture: "):
                    arch = line.split(": ", 1)[1]
                if ver and arch:
                    break

            deb_filename = f"{p}_{ver.replace(':', '%3a')}_{arch}.deb"
            cache_path = cache_dir / deb_filename

            if not ctx.no_cache and cache_path.exists():
                if ctx.verbose:
                    rprint(f"    [dim]Using cached: {deb_filename}[/dim]")
            else:
                if ctx.verbose:
                    action = "Forced re-download" if ctx.no_cache else "Downloading"
                    rprint(f"    [dim]{action}: {p}...[/dim]")
                dl_res = _run(["apt-get", "download", p], cwd=str(cache_dir))
                if dl_res.returncode != 0:
                    errors.append(
                        f"[apt] apt-get download failed for '{p}': {dl_res.stderr.strip()}"
                    )
                    continue

            # Copy from cache to bundle debs dir
            if cache_path.exists():
                shutil.copy2(str(cache_path), str(deb_dir / deb_filename))
            else:
                # Fallback: maybe the filename was different than expected?
                # Find the most recently modified .deb in cache_dir starting with p_
                cand = list(cache_dir.glob(f"{p}_*.deb"))
                if cand:
                    latest = max(cand, key=lambda x: x.stat().st_mtime)
                    shutil.copy2(str(latest), str(deb_dir / latest.name))
                else:
                    errors.append(f"[apt] Could not find downloaded .deb for '{p}' in cache")

        # 3. Generate Packages index
        if ctx.verbose:
            rprint("[cyan]→[/] Generating Packages index...")

        packages_file = deb_dir / "Packages"
        sources_file = ctx.bundle_dir / "apt" / "sources.list"
        codename_file = ctx.bundle_dir / "apt" / "pack_codename"

        idx_cmd = ["dpkg-scanpackages", ".", "/dev/null"]
        idx_res = _run(idx_cmd, cwd=str(deb_dir))

        if idx_res.returncode != 0:
            errors.append(f"[apt] dpkg-scanpackages failed: {idx_res.stderr.strip()}")
        elif not idx_res.stdout.strip():
            # If we resolved packages but scanpackages found nothing, that's an error
            if all_pkgs:
                errors.append(
                    "[apt] dpkg-scanpackages produced empty index despite finding packages. Ensure .deb files were downloaded."
                )
        else:
            # Write the package index canonically inside debs/ (so the path is the same as the files)
            packages_file.write_text(idx_res.stdout)
            import gzip

            with gzip.open(str(deb_dir / "Packages.gz"), "wt") as f:
                f.write(idx_res.stdout)

        # 4. Write sources.list fragment (primarily for local apply)
        if ctx.verbose:
            rprint("[cyan]→[/] Writing sources.list...")
        # Using a relative path that local apply can resolve or override
        sources_file.write_text("deb [trusted=yes] file://./debs ./\n")

        # 5. Record target codename for mismatch warning on apply
        codename_file.write_text(_detect_codename())

        artifacts = [str(deb_dir), str(packages_file), str(sources_file)]
        success = len(errors) == 0
        if ctx.verbose and success:
            rprint(f"[green]✓[/] apt: {total_dl} packages downloaded")

        msg = "apt packed successfully" if success else f"apt packed with {len(errors)} error(s)"
        return PluginResult(success=success, message=msg, artifacts=artifacts, errors=errors)

    def apply(self, task_spec: dict[str, Any], ctx: ApplyContext) -> PluginResult:
        bundle_apt = ctx.bundle_dir / "apt"
        deb_dir = bundle_apt / "debs"
        packages_file = bundle_apt / "Packages"

        if not deb_dir.exists() or not packages_file.exists():
            return PluginResult(
                success=False,
                message="[apt] Bundle is missing apt artifacts (debs/ or Packages)",
                artifacts=[],
                errors=["Missing apt artifacts in bundle"],
            )

        # Warn on codename mismatch
        pack_codename_file = bundle_apt / "pack_codename"
        if pack_codename_file.exists():
            pack_cn = pack_codename_file.read_text().strip()
            apply_cn = _detect_codename()
            if pack_cn and apply_cn and pack_cn != apply_cn:
                import sys

                print(
                    f"[apt] WARNING: bundle was packed on '{pack_cn}' but applying on '{apply_cn}'. "
                    "Package compatibility is not guaranteed.",
                    file=sys.stderr,
                )

        packages: list[str] = task_spec.get("packages", [])
        target_dir = Path("/srv/offline/apt")

        if ctx.dry_run:
            return PluginResult(
                success=True,
                message=f"[dry-run] Would set up local apt repo at {target_dir} and install {packages}",
                artifacts=[],
                errors=[],
            )

        # 1. Copy debs to target
        target_debs = target_dir / "debs"
        target_debs.mkdir(parents=True, exist_ok=True)
        shutil.copytree(str(deb_dir), str(target_debs), dirs_exist_ok=True)

        # 2. Regenerate Packages index on target
        idx_cmd = ["dpkg-scanpackages", "debs", "/dev/null"]
        idx_res = _run(idx_cmd, cwd=str(target_dir))
        (target_dir / "Packages").write_text(idx_res.stdout)

        # 3. Write sources.list.d entry
        sources_dir = Path("/etc/apt/sources.list.d")
        sources_dir.mkdir(parents=True, exist_ok=True)
        (sources_dir / "offline.list").write_text("deb [trusted=yes] file:///srv/offline/apt ./\n")

        # 4. Update apt cache
        upd = _run(["apt-get", "update"])
        if upd.returncode != 0:
            return PluginResult(
                success=False,
                message=f"[apt] apt-get update failed: {upd.stderr.strip()}",
                artifacts=[],
                errors=[upd.stderr],
            )

        # 5. Idempotence — filter out already-installed packages at correct version
        to_install: list[str] = []
        for pkg in packages:
            chk = _run(["dpkg", "-l", pkg])
            if chk.returncode == 0 and "ii" in chk.stdout:
                if ctx.verbose:
                    print(f"[apt] Skipping already-installed: {pkg}")
            else:
                to_install.append(pkg)

        if not to_install:
            return PluginResult(
                success=True,
                message="[apt] All packages already installed — nothing to do",
                artifacts=[str(target_dir)],
                errors=[],
            )

        # 6. Install
        inst_cmd = ["apt-get", "install", "-y"] + to_install
        inst_res = _run(inst_cmd)
        if inst_res.returncode != 0:
            return PluginResult(
                success=False,
                message=f"[apt] apt-get install failed\nCommand: {' '.join(inst_cmd)}\n{inst_res.stderr}",
                artifacts=[],
                errors=[inst_res.stderr],
            )

        return PluginResult(
            success=True,
            message=f"[apt] Installed {len(to_install)} package(s) successfully",
            artifacts=[str(target_dir)],
            errors=[],
        )

    def diff(self, old_spec: dict[str, Any] | None, new_spec: dict[str, Any]) -> DiffResult:
        if old_spec is None:
            new_pkgs = new_spec.get("packages", [])
            return DiffResult(
                plugin_name=self.name, added=new_pkgs, removed=[], updated=[], unchanged=[]
            )

        old_set = set(old_spec.get("packages", []))
        new_set = set(new_spec.get("packages", []))

        return DiffResult(
            plugin_name=self.name,
            added=sorted(new_set - old_set),
            removed=sorted(old_set - new_set),
            updated=[],
            unchanged=sorted(old_set & new_set),
        )

    def render_apply_sh(self, task_spec: dict[str, Any], bundle_subdir: str) -> str:
        packages = " ".join(task_spec.get("packages", []))
        return f"""
echo "[apt] Configuring isolated local repository..."
SOURCES_FILE="$BUNDLE_DIR/{bundle_subdir}/syncit.list"
echo "deb [trusted=yes] file://$BUNDLE_DIR/{bundle_subdir}/debs ./" > "$SOURCES_FILE"

echo "[apt] Updating package index (local only)..."
sudo apt-get update \\
  -o Dir::Etc::SourceList="$SOURCES_FILE" \\
  -o Dir::Etc::SourceParts="/dev/null" \\
  -o Dir::Etc::Preferences="/dev/null" \\
  -o Dir::Etc::PreferencesParts="/dev/null" \\
  -o APT::Get::List-Cleanup=0

echo "[apt] Installing packages..."
sudo apt-get install -y --no-install-recommends \\
  -o Dir::Etc::SourceList="$SOURCES_FILE" \\
  -o Dir::Etc::SourceParts="/dev/null" \\
  -o Dir::Etc::Preferences="/dev/null" \\
  -o Dir::Etc::PreferencesParts="/dev/null" \\
  {packages}
"""


registry.register(AptPlugin())
