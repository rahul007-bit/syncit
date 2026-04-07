from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field


class AppliedTask(BaseModel):
    name: str
    plugin: str
    bundle_version: str
    applied_at: datetime
    checksum: str
    # Plugin-specific metadata (packages installed, wheel count, etc.)
    metadata: dict[str, Any] = Field(default_factory=dict)


class BundleState(BaseModel):
    last_bundle: str | None = None
    last_applied_at: datetime | None = None
    applied_tasks: list[AppliedTask] = Field(default_factory=list)

    def get_task(self, name: str) -> AppliedTask | None:
        """Return the applied task record by task name, or None."""
        for task in self.applied_tasks:
            if task.name == name:
                return task
        return None

    def upsert_task(self, task: AppliedTask) -> None:
        """Insert or replace a task record by name."""
        for i, existing in enumerate(self.applied_tasks):
            if existing.name == task.name:
                self.applied_tasks[i] = task
                return
        self.applied_tasks.append(task)


DEFAULT_STATE_FILE = Path("/opt/offlinectl/state.json")


def load_state(state_file: Path) -> BundleState:
    """Load state.json from disk. Returns empty state if file doesn't exist."""
    if not state_file.exists():
        return BundleState()
    with state_file.open("r") as f:
        return BundleState(**json.load(f))


def save_state(state_file: Path, state: BundleState) -> None:
    """Persist state.json to disk."""
    state_file.parent.mkdir(parents=True, exist_ok=True)
    with state_file.open("w") as f:
        f.write(state.model_dump_json(indent=2))
