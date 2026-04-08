"""Pydantic models for role.yaml schema."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class RoleTask(BaseModel):
    """A single task entry inside a role."""

    plugin: str
    name: str
    spec: dict[str, Any] = Field(default_factory=dict)


class Role(BaseModel):
    """Represents a parsed role.yaml file."""

    name: str
    description: str | None = None
    version: str | None = None
    tasks: list[RoleTask] = Field(default_factory=list)
