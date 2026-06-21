"""DNF plugin for offline RPM caching and repo creation."""

from __future__ import annotations

import json
import re
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

        if "base_installroot" in task_spec:
            if not isinstance(task_spec["base_installroot"], str):
                errors.append("[dnf] 'base_installroot' must be a string path")

        if "releasever" in task_spec and not isinstance(task_spec["releasever"], (str, int)):
            errors.append("[dnf] 'releasever' must be a string or integer (e.g. '9' or 9)")

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

        slug = ctx.task_slug or "default"
        dnf_dir = ctx.bundle_dir / slug
        rpm_dir = dnf_dir
        keys_dir = dnf_dir / "keys"
        rpm_dir.mkdir(parents=True, exist_ok=True)

        # Cache directory (used by DNF via --setopt=cachedir for network efficiency)
        cache_dir = Path("~/.cache/syncit/dnf").expanduser()
        cache_dir.mkdir(parents=True, exist_ok=True)

        if ctx.no_cache:
            if ctx.verbose:
                print(f"[dnf] --no-cache: clearing local cache at {cache_dir}...")
            shutil.rmtree(cache_dir, ignore_errors=True)
            cache_dir.mkdir(parents=True, exist_ok=True)

        # ── Phase 0: Process upstream repos ─────────────────────────────────
        extra_repo_opts: list[str] = []
        if repos:
            keys_dir.mkdir(exist_ok=True)
            if ctx.verbose:
                print(f"[dnf] Processing {len(repos)} upstream repo(s)...")

            for repo in repos:
                raw_name = repo.get("name", "repo")
                # Prefix with 'syncit_' to avoid colliding with any repo of the
                # same name already present in /etc/yum.repos.d/ on the build host.
                name = "syncit_" + re.sub(r"[^a-zA-Z0-9._-]", "_", raw_name)
                baseurl = repo.get("baseurl", "")

                # GPG key: record in repos.json for audit trail but do NOT
                # download during pack — keys are only needed at install time
                # (gpgcheck), not during `dnf download`. Some providers (e.g.
                # pkgs.k8s.io) return 403 on direct key URL requests.
                gpgkey_url = repo.get("gpgkey")
                if gpgkey_url and ctx.verbose:
                    print(f"    [dnf] GPG key noted for '{raw_name}' (not downloaded at pack time)")

                # Inject repo via --repofrompath (no system mutation).
                # The repo ID (first token) uses our 'syncit_'-prefixed name so
                # it never conflicts with a system repo of the same name.
                extra_repo_opts.extend(["--repofrompath", f"{name},{baseurl}"])
                # Enable only our injected repos; disable all system repos so
                # the build host's /etc/yum.repos.d/ doesn't interfere.
                extra_repo_opts.extend(["--enablerepo", name])
                if ctx.verbose:
                    print(f"    [dnf] Added repo '{name}' ({baseurl})")

        # ── Phase 1: Download packages ──────────────────────────────────────
        #
        # The correct approach for offline bundling is `dnf download --resolve`
        # combined with `--installroot` pointing at a *minimal base OS root*.
        #
        # Why this is the right model:
        #   - `dnf download --resolve`       → downloads ALL transitive deps,
        #                                       regardless of any installed state.
        #                                       Pulls in dbus, systemd, acl… even
        #                                       though every fresh RHEL/Rocky node
        #                                       already has them. Too much.
        #   - `dnf install --downloadonly`   → only downloads what is missing on
        #                                       the *build* machine. If the build
        #                                       host has extra packages the fresh
        #                                       target won't have, those are silently
        #                                       skipped. Too little, wrong reference.
        #   - `dnf download --resolve \
        #       --installroot <minimal-root>` → DNF resolves as if it is installing
        #                                       into that root. Packages already
        #                                       present there (the minimal base OS:
        #                                       systemd, dbus, acl…) are skipped.
        #                                       Only your app's actual extra deps are
        #                                       downloaded. Correct reference, correct
        #                                       result regardless of the build host.
        #
        # Set `base_installroot` in your manifest task to the path of a minimal
        # base-OS root (e.g. a debootstrapped/dnf-installrooted Rocky 9 tree).
        # If omitted DNF falls back to resolving against the build host (old
        # behaviour, can over- or under-download).
        base_installroot = task_spec.get("base_installroot")
        releasever = task_spec.get("releasever")

        import tempfile

        with tempfile.TemporaryDirectory(prefix="syncit-dnf-") as temp_dir:
            temp_dl_dir = Path(temp_dir)

            dl_cmd = [
                "dnf",
                "download",
                "--resolve",
                "-y",
                f"--setopt=cachedir={cache_dir}",
                "--destdir",
                str(temp_dl_dir),
            ]

            # If the user declared custom repos, disable all system repos and rely
            # only on those injected repos. This prevents conflicts like
            # 'Repository kubernetes is listed more than once' when the build host
            # already has a system-level kubernetes.repo in /etc/yum.repos.d/.
            if repos:
                dl_cmd.insert(2, "--disablerepo=*")

            if base_installroot:
                installroot_path = Path(base_installroot).expanduser().resolve()
                if not installroot_path.is_dir():
                    errors.append(
                        f"[dnf] base_installroot '{installroot_path}' does not exist or is not a directory. "
                        "Create it first with: dnf install --installroot <path> @core -y"
                    )
                    return PluginResult(False, "Invalid base_installroot", artifacts, errors)
                dl_cmd.extend(["--installroot", str(installroot_path)])
                if ctx.verbose:
                    print(f"[dnf] Resolving deps against installroot: {installroot_path}")
            else:
                if ctx.verbose:
                    print(
                        "[dnf] WARNING: no base_installroot set — resolving against build host. "
                        "Set 'base_installroot' in your manifest to a minimal OS root for accurate dep resolution."
                    )

            if releasever:
                dl_cmd.extend(["--releasever", str(releasever)])

            dl_cmd.extend(extra_repo_opts)
            dl_cmd.extend(packages)

            res = subprocess.run(dl_cmd, capture_output=True, text=True)
            if res.returncode != 0:
                errors.append(f"[dnf] download failed: {res.stderr}")
                return PluginResult(False, "Failed to download RPMs", artifacts, errors)

            # Copy all resolved RPMs to the bundle.
            resolved_rpms = [
                f for f in temp_dl_dir.iterdir()
                if f.is_file() and f.suffix == ".rpm"
            ]
            if ctx.verbose:
                print(f"[dnf] Downloaded/resolved {len(resolved_rpms)} RPM(s) to bundle.")
            for f in resolved_rpms:
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

        artifacts.append(str(rpm_dir))

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

        slug = ctx.task_slug or "default"
        bundled_rpms = ctx.bundle_dir / slug
        if not bundled_rpms.exists():
            return PluginResult(False, "No bundled RPMs found", [], ["[dnf] rpms missing"])

        dest_rpm = Path("/srv/offline/dnf") / slug
        repo_path = Path("/etc/yum.repos.d") / f"syncit-{slug}.repo"

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

            repo_content = (
                f"[syncit-{slug}]\n"
                f"name=Syncit Offline Bundle ({slug})\n"
                f"baseurl=file:///srv/offline/dnf/{slug}\n"
                "enabled=1\n"
                "gpgcheck=0\n"
            )
            repo_path.parent.mkdir(parents=True, exist_ok=True)
            repo_path.write_text(repo_content)
            artifacts.append(str(repo_path))

            res_inst = subprocess.run(
                ["dnf", "install", "-y", "--disablerepo=*", f"--enablerepo=syncit-{slug}"] + packages,
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
        # bundle_subdir is e.g. "dnf/install-kubernetes-packages"
        slug = bundle_subdir.split("/", 1)[-1] if "/" in bundle_subdir else bundle_subdir
        return f"""
echo "[dnf] Configuring local repository ({bundle_subdir})..."
mkdir -p /srv/offline/dnf/{slug}
cp -r "$BUNDLE_DIR/{bundle_subdir}"/* /srv/offline/dnf/{slug}/
createrepo_c /srv/offline/dnf/{slug}
cat > /etc/yum.repos.d/syncit-{slug}.repo << 'EOF'
[syncit-{slug}]
name=Syncit Local Repo ({slug})
baseurl=file:///srv/offline/dnf/{slug}
enabled=1
gpgcheck=0
EOF
dnf install -y --disablerepo=* --enablerepo=syncit-{slug} {packages}
"""


registry.register(DnfPlugin())
