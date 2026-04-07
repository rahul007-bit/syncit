"""cargo plugin stub — not implemented in Phase 1."""

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


class CargoPlugin(OfflinePlugin):
    name = "cargo"

    def validate(self, task_spec: dict[str, Any]) -> list[str]:
        return ["[cargo] Plugin is not implemented in Phase 1"]

    def pack(self, task_spec: dict[str, Any], ctx: PackContext) -> PluginResult:
        raise NotImplementedError("cargo plugin is not implemented in Phase 1")

    def apply(self, task_spec: dict[str, Any], ctx: ApplyContext) -> PluginResult:
        raise NotImplementedError("cargo plugin is not implemented in Phase 1")

    def diff(self, old_spec: dict[str, Any] | None, new_spec: dict[str, Any]) -> DiffResult:
        raise NotImplementedError("cargo plugin is not implemented in Phase 1")


registry.register(CargoPlugin())
