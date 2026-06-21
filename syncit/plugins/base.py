from abc import ABC, abstractmethod
from dataclasses import dataclass
import os
from pathlib import Path
import shutil
import subprocess
import sys
from typing import Any, Optional

_sudo_password: Optional[str] = None


def get_sudo_password() -> str:
    global _sudo_password
    if _sudo_password is not None:
        return _sudo_password

    if os.getuid() == 0:
        _sudo_password = ""
        return ""

    import questionary

    passwd = questionary.password("Enter sudo password (required for system changes):").ask()
    if passwd is None:
        print("Sudo password is required to continue.", file=sys.stderr)
        sys.exit(1)
    _sudo_password = passwd
    return _sudo_password


def run_privileged(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
    if os.getuid() == 0:
        return subprocess.run(cmd, **kwargs)

    passwd = get_sudo_password()
    full_cmd = ["sudo", "-S"] + cmd

    if "input" in kwargs:
        kwargs["input"] = f"{passwd}\n{kwargs['input']}"
    else:
        kwargs["input"] = f"{passwd}\n"

    kwargs["text"] = True
    return subprocess.run(full_cmd, **kwargs)


def write_privileged_file(path: Path, content: str) -> None:
    if os.getuid() == 0:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        return

    import tempfile

    with tempfile.NamedTemporaryFile("w", delete=False) as f:
        f.write(content)
        temp_path = f.name

    try:
        run_privileged(["mkdir", "-p", str(path.parent)], check=True, capture_output=True)
        run_privileged(["mv", temp_path, str(path)], check=True, capture_output=True)
        run_privileged(["chmod", "644", str(path)], check=True, capture_output=True)
    finally:
        if os.path.exists(temp_path):
            os.unlink(temp_path)


def copytree_privileged(src: Path, dest: Path) -> None:
    if os.getuid() == 0:
        shutil.copytree(str(src), str(dest), dirs_exist_ok=True)
        return

    run_privileged(["mkdir", "-p", str(dest.parent)], check=True, capture_output=True)
    run_privileged(["cp", "-r", str(src), str(dest)], check=True, capture_output=True)



@dataclass
class PackContext:
    bundle_dir: Path  # Root of the bundle being built
    manifest_dir: Path  # Directory containing bundle.yaml (for relative paths)
    task_slug: str = ""   # URL-safe slug derived from task.name (e.g. "install-kubernetes-packages")
    dry_run: bool = False
    verbose: bool = False
    no_cache: bool = False


@dataclass
class ApplyContext:
    bundle_dir: Path  # Root of the bundle to apply
    state_file: Path  # Path to state.json on offline VM
    task_slug: str = ""   # URL-safe slug derived from task.name (e.g. "install-kubernetes-packages")
    dry_run: bool = False
    verbose: bool = False
    force: bool = False  # Re-apply even if state says already done


@dataclass
class DiffResult:
    plugin_name: str
    added: list[str]
    removed: list[str]
    updated: list[str]  # e.g. version changed
    unchanged: list[str]


@dataclass
class PluginResult:
    success: bool
    message: str
    artifacts: list[str]  # paths written to bundle_dir (for pack) or applied (for apply)
    errors: list[str]


class OfflinePlugin(ABC):
    name: str  # Unique plugin identifier, matches `plugin:` in manifest

    @abstractmethod
    def validate(self, task_spec: dict[str, Any]) -> list[str]:
        """
        Validate task spec fields.
        Returns list of error strings. Empty list = valid.
        Called before pack or apply.
        """
        pass

    @abstractmethod
    def pack(self, task_spec: dict[str, Any], ctx: PackContext) -> PluginResult:
        """
        Run on the ONLINE VM.
        Download/resolve all dependencies into ctx.bundle_dir/<plugin_name>/.
        Must be idempotent. Must respect ctx.dry_run.
        """
        pass

    @abstractmethod
    def apply(self, task_spec: dict[str, Any], ctx: ApplyContext) -> PluginResult:
        """
        Run on the OFFLINE VM.
        Configure system to use local bundle artifacts.
        Must be idempotent. Must respect ctx.dry_run.
        """
        pass

    @abstractmethod
    def diff(self, old_spec: dict[str, Any] | None, new_spec: dict[str, Any]) -> DiffResult:
        """
        Compare two versions of this plugin's task spec.
        Used by `syncit diff`.
        old_spec=None means this task is brand new.
        """
        pass

    @abstractmethod
    def render_apply_sh(self, task_spec: dict[str, Any], bundle_subdir: str) -> str:
        """
        Generate a bash strictly relying on native OS dependencies for zero-dependency remote apply.
        `bundle_subdir` is the sub-directory path for this task within `$BUNDLE_DIR`
        (e.g. "apt/install-kubernetes-packages").
        """
        pass
