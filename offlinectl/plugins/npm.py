"""npm plugin stub — not implemented in Phase 1."""

from __future__ import annotations

from typing import Any

from offlinectl.plugins.base import (
    ApplyContext,
    DiffResult,
    OfflinePlugin,
    PackContext,
    PluginResult,
)
from offlinectl.plugins.registry import registry


class NpmPlugin(OfflinePlugin):
    name = "npm"

    def validate(self, task_spec: dict[str, Any]) -> list[str]:
        return ["[npm] Plugin is not implemented in Phase 1"]

    def pack(self, task_spec: dict[str, Any], ctx: PackContext) -> PluginResult:
        raise NotImplementedError("npm plugin is not implemented in Phase 1")

    def apply(self, task_spec: dict[str, Any], ctx: ApplyContext) -> PluginResult:
        raise NotImplementedError("npm plugin is not implemented in Phase 1")

    def diff(self, old_spec: dict[str, Any] | None, new_spec: dict[str, Any]) -> DiffResult:
        raise NotImplementedError("npm plugin is not implemented in Phase 1")


registry.register(NpmPlugin())
