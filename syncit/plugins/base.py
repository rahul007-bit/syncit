from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Any


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
