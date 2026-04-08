from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, field_validator


class ManifestMetadata(BaseModel):
    name: str
    version: str
    description: str | None = None
    author: str | None = None


class TargetSpec(BaseModel):
    distro: str
    codename: str
    arch: str


class TaskSpec(BaseModel):
    name: str
    plugin: str
    # Everything beyond name/plugin becomes the task config (passed to plugins)
    config: dict[str, Any] = Field(default_factory=dict)

    @classmethod
    def from_yaml_task(cls, task_dict: dict[str, Any]) -> TaskSpec:
        data = task_dict.copy()
        name = data.pop("name")
        plugin = data.pop("plugin")
        return cls(name=name, plugin=plugin, config=data)


class BundleSpec(BaseModel):
    targets: TargetSpec
    roles: list[dict[str, Any]] = Field(default_factory=list)
    tasks: list[dict[str, Any]] = Field(default_factory=list)


class BundleManifest(BaseModel):
    apiVersion: str
    kind: str
    metadata: ManifestMetadata
    spec: BundleSpec

    @field_validator("apiVersion")
    @classmethod
    def check_api_version(cls, v: str) -> str:
        if v != "offlinectl/v1":
            raise ValueError(f"Unsupported apiVersion '{v}'. Expected 'offlinectl/v1'.")
        return v

    @field_validator("kind")
    @classmethod
    def check_kind(cls, v: str) -> str:
        if v != "Bundle":
            raise ValueError(f"Unsupported kind '{v}'. Expected 'Bundle'.")
        return v

    def get_targets(self) -> TargetSpec:
        return self.spec.targets

    def get_tasks(self) -> list[TaskSpec]:
        return [TaskSpec.from_yaml_task(task) for task in self.spec.tasks]
