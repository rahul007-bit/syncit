from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel


class BundleTaskMeta(BaseModel):
    name: str
    plugin: str
    status: str  # "packed" | "failed"
    artifact_count: int = 0


class BundleMetadata(BaseModel):
    name: str
    version: str
    created_at: datetime
    syncit_version: str
    targets: dict[str, str]
    tasks: list[BundleTaskMeta] = []


def write_meta(bundle_dir: Path, meta: BundleMetadata) -> None:
    """Write bundle.meta.json to the bundle directory."""
    meta_file = bundle_dir / "bundle.meta.json"
    with meta_file.open("w") as f:
        f.write(meta.model_dump_json(indent=2))


def read_meta(bundle_dir: Path) -> BundleMetadata:
    """Read bundle.meta.json from the bundle directory."""
    meta_file = bundle_dir / "bundle.meta.json"
    if not meta_file.exists():
        raise FileNotFoundError(f"bundle.meta.json not found in {bundle_dir}")
    with meta_file.open("r") as f:
        return BundleMetadata(**json.load(f))


def compute_task_checksum(bundle_dir: Path, plugin_name: str) -> str:
    """Compute a SHA-256 checksum of all files in the plugin's artifact directory."""
    plugin_dir = bundle_dir / plugin_name
    if not plugin_dir.exists():
        return "sha256:"

    sha = hashlib.sha256()
    for file in sorted(plugin_dir.rglob("*")):
        if file.is_file():
            sha.update(str(file.relative_to(bundle_dir)).encode())
            sha.update(file.read_bytes())
    return f"sha256:{sha.hexdigest()}"


def bundle_dir_name(name: str, version: str) -> str:
    """Return the canonical bundle directory name."""
    import re
    name = re.sub(r"[/\\]", "_", name)
    version = re.sub(r"[/\\]", "_", version)
    return f"bundle-{name}-{version}"


def now_utc() -> datetime:
    return datetime.now(UTC)
