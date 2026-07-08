"""pip plugin — downloads Python wheels and installs them offline."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
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


def _pip_executable() -> list[str]:
    # Always prefer system pip3/pip — never use sys.executable
    for candidate in ["pip3", "pip"]:
        if shutil.which(candidate):
            return [candidate]
    # Last resort: find system python3 (not sys.executable) and use its -m pip
    python3 = shutil.which("python3")
    if python3:
        result = subprocess.run([python3, "-m", "pip", "--version"], capture_output=True)
        if result.returncode == 0:
            return [python3, "-m", "pip"]
    raise RuntimeError("pip not found — install pip3: sudo apt-get install python3-pip")


class PipPlugin(OfflinePlugin):
    name = "pip"

    def validate(self, task_spec: dict[str, Any]) -> list[str]:
        errors: list[str] = []
        try:
            _pip_executable()
        except RuntimeError as e:
            errors.append(f"[pip] {e}")
        if "requirements" not in task_spec and "pyproject" not in task_spec:
            errors.append("[pip] Task spec must contain either 'requirements' or 'pyproject' path")
        for field in ("requirements", "pyproject", "python_version"):
            if field in task_spec and not isinstance(task_spec[field], str):
                errors.append(f"[pip] Field '{field}' must be a string")
        return errors

    def pack(self, task_spec: dict[str, Any], ctx: PackContext) -> PluginResult:
        # Resolve the requirements file (relative to manifest dir)
        req_path: Path | None = None
        if "requirements" in task_spec:
            req_path = (ctx.manifest_dir / task_spec["requirements"]).resolve()
        elif "pyproject" in task_spec:
            return PluginResult(
                success=False,
                message="[pip] pyproject.toml support is planned for Phase 2",
                artifacts=[],
                errors=["Only requirements.txt is supported in Phase 1"],
            )

        python_version = task_spec.get("python_version", f"{sys.version_info.major}.{sys.version_info.minor}")

        slug = ctx.task_slug or "default"
        wheel_dir = ctx.bundle_dir / slug

        if ctx.dry_run:
            return PluginResult(
                success=True,
                message=(
                    f"[dry-run] Would run: pip download -r {req_path} "
                    f"--python-version {python_version} --only-binary=:all: --dest {wheel_dir}"
                ),
                artifacts=[],
                errors=[],
            )

        if not req_path or not req_path.exists():
            return PluginResult(
                success=False,
                message=f"[pip] Requirements file not found: {req_path}",
                artifacts=[],
                errors=[f"File not found: {req_path}"],
            )

        wheel_dir.mkdir(parents=True, exist_ok=True)

        # Cache directory
        cache_dir = Path("~/.cache/syncit/pip").expanduser()
        cache_dir.mkdir(parents=True, exist_ok=True)

        # First attempt: binary-only (preferred for air-gap compatibility)
        cmd = [
            *_pip_executable(),
            "download",
            "-r",
            str(req_path),
            "--python-version",
            python_version,
            "--only-binary=:all:",
            "--dest",
            str(wheel_dir),
            "--cache-dir",
            str(cache_dir),
        ]
        if ctx.no_cache:
            cmd.append("--no-cache-dir")

        def do_run(run_cmd: list[str]) -> subprocess.CompletedProcess:
            if ctx.verbose:
                from rich import print as rprint

                rprint(f"[cyan]→[/] Running: {' '.join(run_cmd)}")
                output = []
                with subprocess.Popen(
                    run_cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True
                ) as proc:
                    if proc.stdout:
                        for line in iter(proc.stdout.readline, ""):
                            output.append(line)
                            rprint(f"[cyan]→[/] {line.rstrip()}")
                proc.wait()
                return subprocess.CompletedProcess(
                    args=run_cmd, returncode=proc.returncode, stdout="".join(output), stderr=""
                )
            else:
                return _run(run_cmd)

        result = do_run(cmd)

        if result.returncode != 0:
            # Retry without --only-binary, warn user
            print(
                "[pip] WARNING: --only-binary=:all: failed — retrying without it. "
                "Source distributions will be compiled on the offline VM.",
                file=sys.stderr,
            )
            cmd_retry = [c for c in cmd if c != "--only-binary=:all:"]
            result = do_run(cmd_retry)
            if result.returncode != 0:
                return PluginResult(
                    success=False,
                    message=f"[pip] pip download failed\nCommand: {' '.join(cmd_retry)}\n{result.stderr}",
                    artifacts=[],
                    errors=[result.stderr],
                )

        # Copy requirements file into bundle
        bundle_req = ctx.bundle_dir / slug / "requirements.txt"
        shutil.copy2(str(req_path), str(bundle_req))

        artifacts = [str(wheel_dir), str(bundle_req)]
        if ctx.verbose:
            from rich import print as rprint

            rprint("[green]✓[/] pip: wheels downloaded")
        return PluginResult(
            success=True,
            message="[pip] Wheels downloaded successfully",
            artifacts=artifacts,
            errors=[],
        )

    def apply(self, task_spec: dict[str, Any], ctx: ApplyContext) -> PluginResult:
        slug = ctx.task_slug or "default"
        wheel_dir = ctx.bundle_dir / slug
        req_file = ctx.bundle_dir / slug / "requirements.txt"

        if not wheel_dir.exists() or not req_file.exists():
            return PluginResult(
                success=False,
                message="[pip] Bundle is missing pip artifacts (wheels/ or requirements.txt)",
                artifacts=[],
                errors=["Missing pip artifacts in bundle"],
            )

        target_wheel_dir = Path("/srv/offline/pip") / slug
        target_req_file = Path("/srv/offline/pip") / slug / "requirements.txt"

        if ctx.dry_run:
            return PluginResult(
                success=True,
                message=f"[dry-run] Would install wheels from {wheel_dir} → {target_wheel_dir}",
                artifacts=[],
                errors=[],
            )

        # 1. Copy artifacts to target
        target_wheel_dir.mkdir(parents=True, exist_ok=True)
        shutil.copytree(str(wheel_dir), str(target_wheel_dir), dirs_exist_ok=True)
        shutil.copy2(str(req_file), str(target_req_file))

        # 2. Write pip.conf to disable index and point to local wheelhouse
        pip_conf = Path("/etc/pip.conf")
        pip_conf.write_text(f"[global]\nno-index = true\nfind-links = {target_wheel_dir}\n")

        # 3. Idempotence: parse currently installed packages
        installed: dict[str, str] = {}
        list_res = _run([*_pip_executable(), "list", "--format=json"])
        if list_res.returncode == 0:
            try:
                for entry in json.loads(list_res.stdout):
                    installed[entry["name"].lower()] = entry["version"]
            except (json.JSONDecodeError, KeyError):
                pass

        # 4. Install from local wheelhouse
        install_cmd = [
            *_pip_executable(),
            "install",
            "--no-index",
            "--find-links",
            str(target_wheel_dir),
            "-r",
            str(target_req_file),
        ]

        def do_run_install(run_cmd: list[str]) -> subprocess.CompletedProcess:
            if ctx.verbose:
                from rich import print as rprint

                rprint(f"[cyan]→[/] Running: {' '.join(run_cmd)}")
                output = []
                with subprocess.Popen(
                    run_cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True
                ) as proc:
                    if proc.stdout:
                        for line in iter(proc.stdout.readline, ""):
                            output.append(line)
                            rprint(f"[cyan]→[/] {line.rstrip()}")
                proc.wait()
                return subprocess.CompletedProcess(
                    args=run_cmd, returncode=proc.returncode, stdout="".join(output), stderr=""
                )
            else:
                return _run(run_cmd)

        result = do_run_install(install_cmd)
        if result.returncode != 0:
            return PluginResult(
                success=False,
                message=f"[pip] pip install failed\nCommand: {' '.join(install_cmd)}\n{result.stderr}",
                artifacts=[],
                errors=[result.stderr],
            )

        if ctx.verbose:
            from rich import print as rprint

            rprint("[green]✓[/] pip: packages installed")

        return PluginResult(
            success=True,
            message="[pip] Packages installed successfully from local wheelhouse",
            artifacts=[str(target_wheel_dir), str(pip_conf)],
            errors=[],
        )

    def diff(self, old_spec: dict[str, Any] | None, new_spec: dict[str, Any]) -> DiffResult:
        """Phase 1 diff: compare requirements file path / spec equality."""
        if old_spec is None:
            return DiffResult(
                plugin_name=self.name,
                added=["(all requirements)"],
                removed=[],
                updated=[],
                unchanged=[],
            )
        if old_spec == new_spec:
            return DiffResult(
                plugin_name=self.name,
                added=[],
                removed=[],
                updated=[],
                unchanged=["(spec unchanged)"],
            )
        return DiffResult(
            plugin_name=self.name,
            added=[],
            removed=[],
            updated=["(requirements spec changed)"],
            unchanged=[],
        )

    def render_apply_sh(self, task_spec: dict[str, Any], bundle_subdir: str) -> str:
        return f"""
echo "[pip] Installing wheels..."
# Check Python version compatibility
PYTHON_VERSION=$(python3 -c 'import sys; print(f"{{sys.version_info.major}}{{sys.version_info.minor}}")')
if ! ls "$BUNDLE_DIR/{bundle_subdir}"/*"cp${{PYTHON_VERSION}}"* >/dev/null 2>&1 && ! ls "$BUNDLE_DIR/{bundle_subdir}"/*"py3-none-any"* >/dev/null 2>&1; then
    echo "[pip] WARNING: No wheels found matching Python version ${{PYTHON_VERSION}}. Installation might fail if native extensions are required."
fi

pip3 install --no-index --find-links "$BUNDLE_DIR/{bundle_subdir}" -r "$BUNDLE_DIR/{bundle_subdir}/requirements.txt"
"""


registry.register(PipPlugin())
