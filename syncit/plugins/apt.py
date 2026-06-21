"""apt plugin — downloads .deb packages and sets up a local apt repository."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
import urllib.request
from pathlib import Path
from typing import Any

from syncit.plugins.base import (
    ApplyContext,
    DiffResult,
    OfflinePlugin,
    PackContext,
    PluginResult,
    copytree_privileged,
    run_privileged,
    write_privileged_file,
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

        if "base_installroot" in task_spec:
            if not isinstance(task_spec["base_installroot"], str):
                errors.append("[apt] 'base_installroot' must be a string path")

        # Validate optional repos
        repos = task_spec.get("repos", [])
        if repos:
            if not isinstance(repos, list):
                errors.append("[apt] 'repos' must be a list")
            else:
                has_gpg_tool = shutil.which("gpg") is not None
                has_download_tool = (
                    shutil.which("curl") is not None or shutil.which("wget") is not None
                )
                for i, repo in enumerate(repos):
                    if not isinstance(repo, dict):
                        errors.append(f"[apt] repos[{i}] must be an object (with 'name' and 'url')")
                        continue
                    if "name" not in repo or not isinstance(repo.get("name"), str):
                        errors.append(f"[apt] repos[{i}] missing required string field: 'name'")
                    if "url" not in repo or not isinstance(repo.get("url"), str):
                        errors.append(f"[apt] repos[{i}] missing required string field: 'url'")
                    if repo.get("gpg_key") and not has_gpg_tool:
                        errors.append(
                            f"[apt] repos[{i}]['{repo['name']}'] has 'gpg_key' but 'gpg' is not installed"
                        )
                    if repo.get("gpg_key") and not has_download_tool:
                        errors.append(
                            f"[apt] repos[{i}]['{repo['name']}'] has 'gpg_key' but neither 'curl' nor 'wget' is installed"
                        )
        return errors

    def pack(self, task_spec: dict[str, Any], ctx: PackContext) -> PluginResult:
        packages: list[str] = task_spec.get("packages", [])
        repos: list[dict[str, Any]] = task_spec.get("repos", [])
        slug = ctx.task_slug or "default"
        apt_dir = ctx.bundle_dir / slug
        deb_dir = apt_dir
        keys_dir = apt_dir / "keys"
        deb_dir.mkdir(parents=True, exist_ok=True)

        if ctx.dry_run:
            msg = f"[dry-run] Would download {len(packages)} package(s) + dependencies"
            if repos:
                repo_names = ", ".join(r.get("name", "?") for r in repos)
                msg += f" from repos: {repo_names}"
            return PluginResult(success=True, message=msg, artifacts=[], errors=[])

        errors: list[str] = []
        all_pkgs: set[str] = set()
        temp_sources: str | None = None
        extra_opts: list[str] = []

        # ── Phase 0: Process upstream repos ─────────────────────────────────
        if repos:
            keys_dir.mkdir(exist_ok=True)
            temp_sources = tempfile.mkdtemp(prefix="syncit-apt-")
            temp_sources_d = Path(temp_sources) / "sources.list.d"
            temp_sources_d.mkdir(parents=True, exist_ok=True)

            # Copy main sources.list if it exists
            main_sources = Path("/etc/apt/sources.list")
            if main_sources.exists():
                shutil.copy2(str(main_sources), str(Path(temp_sources) / "sources.list"))

            if ctx.verbose:
                from rich import print as rprint

                rprint(f"[cyan]→[/] Processing {len(repos)} upstream repo(s)...")

            for repo in repos:
                name = repo.get("name", "repo")
                name = re.sub(r"[^a-zA-Z0-9._-]", "_", name)
                repo_url = repo.get("url", "")

                # Download GPG key into bundle for audit trail
                gpg_key_url = repo.get("gpg_key")
                if gpg_key_url:
                    key_dest = keys_dir / f"{name}.gpg"
                    if not key_dest.exists() or ctx.no_cache:
                        try:
                            if ctx.verbose:
                                rprint(f"    [dim]Downloading GPG key for '{name}'...[/dim]")
                            if not (gpg_key_url.startswith("http://") or gpg_key_url.startswith("https://")):
                                raise ValueError(f"Invalid URL scheme (only http/https allowed): {gpg_key_url}")
                            urllib.request.urlretrieve(gpg_key_url, str(key_dest))
                            # Check if ASCII-armored and dearmor if so
                            raw = key_dest.read_bytes()
                            if raw.startswith(b"-----BEGIN PGP PUBLIC KEY BLOCK-----"):
                                armored = key_dest.with_suffix(".gpg.asc")
                                key_dest.rename(armored)
                                _run(
                                    ["gpg", "--dearmor", "--yes", "-o", str(key_dest), str(armored)]
                                )
                                armored.unlink(missing_ok=True)
                        except Exception as exc:
                            errors.append(
                                f"[apt] Failed to download GPG key for repo '{name}': {exc}"
                            )

                # Inject [trusted=yes] so apt doesn't require GPG verification during download only
                # The GPG key is stored in the bundle for offline use / audit
                entry = repo_url
                if "[trusted=yes]" not in entry and "[trusted=yes" not in entry:
                    # Insert after 'deb' (with or without options already)
                    if entry.strip().startswith("deb ["):
                        # Already has options — insert trusted=yes
                        entry = entry.replace("deb [", "deb [trusted=yes ", 1)
                    else:
                        entry = entry.replace("deb ", "deb [trusted=yes] ", 1)

                (temp_sources_d / f"{name}.list").write_text(entry + "\n")
                if ctx.verbose:
                    rprint(f"    [dim]Added repo '{name}' for pack[/dim]")

            # Also include system sources.d so base OS repos resolve transitive deps
            sys_sources_d = Path("/etc/apt/sources.list.d")
            if sys_sources_d.exists():
                for f in sys_sources_d.glob("*.list"):
                    shutil.copy2(str(f), str(temp_sources_d / f.name))
                # Ubuntu 24.04+ uses deb822 .sources format
                for f in sys_sources_d.glob("*.sources"):
                    shutil.copy2(str(f), str(temp_sources_d / f.name))

            # Set up user-space caches
            apt_cache_root = Path("~/.cache/syncit/apt-root").expanduser()
            state_dir = apt_cache_root / "state"
            cache_dir = apt_cache_root / "cache"
            state_dir.mkdir(parents=True, exist_ok=True)
            cache_dir.mkdir(parents=True, exist_ok=True)

            extra_opts = [
                "-o", f"Dir::State={state_dir}",
                "-o", f"Dir::Cache={cache_dir}",
            ]
            if (Path(temp_sources) / "sources.list").exists():
                extra_opts.extend(["-o", f"Dir::Etc::SourceList={Path(temp_sources) / 'sources.list'}"])
            extra_opts.extend(["-o", f"Dir::Etc::SourceParts={temp_sources_d}"])

            # apt-get update with combined sources so the cache knows about custom repos
            upd = _run(["apt-get", "update"] + extra_opts, timeout=120)
            if upd.returncode != 0:
                print(f"[apt] WARNING: apt-get update returned non-zero code: {upd.stderr.strip()}", file=sys.stderr)

        try:
            # ── Phase 1: Resolve dependencies for each package ──
            if ctx.verbose:
                from rich import print as rprint

                rprint(f"[cyan]→[/] Resolving dependencies for: {' '.join(packages)}...")

            base_installroot = task_spec.get("base_installroot")

            # Use APT's native solver to find exactly what needs to be downloaded
            install_cmd = [
                "apt-get",
                "install",
                "--print-uris",
                "-qq",
                "--no-install-recommends",
                "-y",
            ] + extra_opts

            if base_installroot:
                installroot_path = Path(base_installroot).expanduser().resolve()
                if not installroot_path.is_dir():
                    errors.append(
                        f"[apt] base_installroot '{installroot_path}' does not exist or is not a directory."
                    )
                    return PluginResult(success=False, message="Invalid base_installroot", artifacts=[], errors=errors)

                status_file = installroot_path / "var" / "lib" / "dpkg" / "status"
                if not status_file.exists():
                    errors.append(
                        f"[apt] base_installroot missing status file at {status_file}. "
                        "Ensure this is a valid Ubuntu/Debian root."
                    )
                    return PluginResult(success=False, message="Invalid base_installroot", artifacts=[], errors=errors)

                install_cmd.extend([
                    "-o", f"Dir::State::status={status_file}",
                    # Disable binary caches so APT is forced to read the custom status file fresh
                    # otherwise it will use the host's installed state and skip required dependencies
                    "-o", "Dir::Cache::pkgcache=",
                    "-o", "Dir::Cache::srcpkgcache="
                ])
                if ctx.verbose:
                    rprint(f"[apt] Resolving deps against installroot: {installroot_path}")
            else:
                if ctx.verbose:
                    rprint(
                        "[apt] WARNING: no base_installroot set — resolving against build host's dpkg status. "
                        "Set 'base_installroot' in your manifest to a minimal OS root for accurate dep resolution."
                    )

            install_cmd.extend(packages)

            res = _run(install_cmd)
            if res.returncode != 0:
                errors.append(f"[apt] apt-get install failed to resolve packages: {res.stderr.strip()}")
                return PluginResult(success=False, message="Dependency resolution failed", artifacts=[], errors=errors)

            # Output lines look like:
            # 'http://archive.ubuntu.com/.../podman_4.9.3_amd64.deb' podman_4.9.3_amd64.deb 13408626 MD5Sum:...
            download_targets: set[str] = set()
            for line in res.stdout.splitlines():
                line = line.strip()
                if not line or not line.startswith("'"):
                    continue
                parts = line.split()
                if len(parts) >= 2:
                    filename = parts[1]
                    # Reconstruct exact pkg=version from the filename to preserve strict version pins
                    m = re.match(r"^([^_]+)_([^_]+)_[^.]+\.deb$", filename)
                    if m:
                        pkg_name = m.group(1)
                        # apt-get download needs epochs unescaped (e.g. %3a -> :)
                        version = m.group(2).replace("%3a", ":")
                        download_targets.add(f"{pkg_name}={version}")
                    else:
                        # Fallback for unusually named packages
                        pkg_name = filename.split("_", 1)[0]
                        download_targets.add(pkg_name)

            # Always ensure explicitly requested packages are included in the download list
            # (In case they were skipped by apt because they were present in the status file)
            explicit_names = {p.split("=")[0].split("/")[0].split(":")[0]: p for p in packages}
            resolved_names = {t.split("=")[0] for t in download_targets}
            
            for name, full_p in explicit_names.items():
                if name not in resolved_names:
                    download_targets.add(full_p)

            sorted_pkgs = sorted(list(download_targets))
            total_dl = len(sorted_pkgs)
            if ctx.verbose:
                rprint(f"[cyan]→[/] Resolved {total_dl} packages to download")

            # ── Phase 2: Download all resolved packages with caching ──────
            cache_dir = Path("~/.cache/syncit/apt").expanduser()
            cache_dir.mkdir(parents=True, exist_ok=True)

            for i, p in enumerate(sorted_pkgs, start=1):
                if ctx.verbose:
                    rprint(f"[cyan]→[/] Processing: {p} ({i}/{total_dl})...")

                p_name = p.split("=")[0]
                version = p.split("=")[1] if "=" in p else None
                
                if version:
                    enc_version = version.replace(":", "%3a")
                    cand = list(cache_dir.glob(f"{p_name}_{enc_version}_*.deb"))
                else:
                    cand = list(cache_dir.glob(f"{p_name}_*.deb"))

                if not ctx.no_cache and cand:
                    latest = max(cand, key=lambda x: x.stat().st_mtime)
                    if ctx.verbose:
                        rprint(f"    [dim]Using cached: {latest.name}[/dim]")
                    shutil.copy2(str(latest), str(deb_dir / latest.name))
                    continue

                if ctx.verbose:
                    action = "Forced re-download" if ctx.no_cache else "Downloading"
                    rprint(f"    [dim]{action}: {p}...[/dim]")
                
                with tempfile.TemporaryDirectory() as td:
                    dl_res = _run(["apt-get", "download", p] + extra_opts, cwd=td)
                    if dl_res.returncode != 0:
                        errors.append(f"[apt] apt-get download failed for '{p}': {dl_res.stderr.strip()}")
                        continue
                        
                    downloaded_files = list(Path(td).glob("*.deb"))
                    if not downloaded_files:
                        errors.append(f"[apt] apt-get download succeeded but no .deb found for '{p}'")
                        continue
                        
                    dl_file = downloaded_files[0]
                    cache_target = cache_dir / dl_file.name
                    
                    # Store in cache and copy to bundle
                    shutil.copy2(str(dl_file), str(cache_target))
                    shutil.copy2(str(dl_file), str(deb_dir / dl_file.name))

            # ── Phase 3: Generate Packages index ──────────────────────────
            if ctx.verbose:
                rprint("[cyan]→[/] Generating Packages index...")

            packages_file = deb_dir / "Packages"
            sources_file = apt_dir / "sources.list"
            codename_file = apt_dir / "pack_codename"

            idx_cmd = ["dpkg-scanpackages", ".", "/dev/null"]
            idx_res = _run(idx_cmd, cwd=str(deb_dir))

            if idx_res.returncode != 0:
                errors.append(f"[apt] dpkg-scanpackages failed: {idx_res.stderr.strip()}")
            elif not idx_res.stdout.strip():
                if all_pkgs:
                    errors.append(
                        "[apt] dpkg-scanpackages produced empty index despite finding packages. "
                        "Ensure .deb files were downloaded."
                    )
            else:
                packages_file.write_text(idx_res.stdout)
                import gzip

                with gzip.open(str(deb_dir / "Packages.gz"), "wt") as pkg_gz:
                    pkg_gz.write(idx_res.stdout)

            # ── Phase 4: Write sources.list fragment (for local apply) ────
            if ctx.verbose:
                rprint("[cyan]→[/] Writing sources.list...")
            sources_file.write_text("deb [trusted=yes] file://./ ./\n")

            # ── Phase 5: Record target codename ──────────────────────────
            codename_file.write_text(_detect_codename())

            # ── Phase 6: Persist repo metadata for audit / diff ──────────
            if repos:
                meta = []
                for repo in repos:
                    repo_entry: dict[str, Any] = {
                        "name": repo["name"],
                        "url": repo["url"],
                    }
                    if repo.get("gpg_key"):
                        repo_entry["gpg_key"] = {
                            "url": repo["gpg_key"],
                            "bundle_path": f"{slug}/keys/{repo['name']}.gpg",
                        }
                    meta.append(repo_entry)
                (apt_dir / "repos.json").write_text(json.dumps(meta, indent=2))

            artifacts = [str(deb_dir), str(packages_file), str(sources_file)]
            success = len(errors) == 0
            if ctx.verbose and success:
                rprint(f"[green]✓[/] apt: {total_dl} packages downloaded")

            msg = (
                "apt packed successfully" if success else f"apt packed with {len(errors)} error(s)"
            )
            return PluginResult(success=success, message=msg, artifacts=artifacts, errors=errors)

        finally:
            # Cleanup temp sources directory
            if temp_sources is not None:
                shutil.rmtree(temp_sources, ignore_errors=True)

    def apply(self, task_spec: dict[str, Any], ctx: ApplyContext) -> PluginResult:
        slug = ctx.task_slug or "default"
        bundle_apt = ctx.bundle_dir / slug
        deb_dir = bundle_apt
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
        target_dir = Path("/srv/offline/apt") / slug

        if ctx.dry_run:
            return PluginResult(
                success=True,
                message=f"[dry-run] Would set up local apt repo at {target_dir} and install {packages}",
                artifacts=[],
                errors=[],
            )

        # 1. Copy bundle_apt to target
        copytree_privileged(bundle_apt, target_dir)

        # 3. Write sources.list.d entry
        sources_dir = Path("/etc/apt/sources.list.d")
        write_privileged_file(sources_dir / f"offline-{slug}.list", f"deb [trusted=yes] file://{target_dir} ./\n")

        # 4. Update apt cache
        upd = run_privileged(["apt-get", "update"], capture_output=True)
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
        inst_cmd = ["apt-get", "install", "-y", "--no-install-recommends"] + to_install
        inst_res = run_privileged(inst_cmd, capture_output=True)
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
            new_repos = new_spec.get("repos", [])
            added = [f"[repo] {r['name']} ({r.get('url', '')})" for r in new_repos] + new_pkgs
            return DiffResult(
                plugin_name=self.name, added=added, removed=[], updated=[], unchanged=[]
            )

        # Compare repos
        old_repos = {(r.get("name", ""), r.get("url", "")) for r in old_spec.get("repos", [])}
        new_repos = {(r.get("name", ""), r.get("url", "")) for r in new_spec.get("repos", [])}

        added_repos = sorted(new_repos - old_repos)
        removed_repos = sorted(old_repos - new_repos)

        added = [f"[repo] {n} ({u})" for n, u in added_repos]
        removed = [f"[repo] {n} ({u})" for n, u in removed_repos]

        # Compare packages
        old_pkgs = set(old_spec.get("packages", []))
        new_pkgs = set(new_spec.get("packages", []))

        added += sorted(new_pkgs - old_pkgs)
        removed += sorted(old_pkgs - new_pkgs)

        return DiffResult(
            plugin_name=self.name,
            added=added,
            removed=removed,
            updated=[],
            unchanged=sorted(old_pkgs & new_pkgs),
        )

    def render_apply_sh(self, task_spec: dict[str, Any], bundle_subdir: str) -> str:
        packages = " ".join(task_spec.get("packages", []))
        return f"""
echo "[apt] Configuring isolated local repository ({bundle_subdir})..."
SOURCES_FILE="$BUNDLE_DIR/{bundle_subdir}/syncit.list"
echo "deb [trusted=yes] file://$BUNDLE_DIR/{bundle_subdir} ./" > "$SOURCES_FILE"

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
