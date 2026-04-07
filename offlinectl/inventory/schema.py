from __future__ import annotations

from pydantic import BaseModel, Field


class Host(BaseModel):
    """Pydantic model representing an offline target VM."""

    host: str
    user: str
    ssh_key: str | None = None
    bundle_dest: str = "/opt/bundles/"
    state_file: str = "/opt/offlinectl/state.json"


class Inventory(BaseModel):
    """Pydantic model representing the offline targets inventory."""

    hosts: dict[str, Host] = Field(default_factory=dict)
    groups: dict[str, list[str]] = Field(default_factory=dict)
