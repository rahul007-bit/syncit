"""Remote state models for Smart Apply idempotency."""

from __future__ import annotations

from typing import Dict

from pydantic import BaseModel, Field


class TaskState(BaseModel):
    checksum: str
    status: str  # 'success' or 'failed'


class RemoteState(BaseModel):
    applied_tasks: Dict[str, TaskState] = Field(default_factory=dict)
