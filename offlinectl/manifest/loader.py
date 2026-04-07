from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import ValidationError

from offlinectl.manifest.schema import BundleManifest


def load_manifest(path: Path) -> BundleManifest:
    """Load and validate a bundle.yaml manifest.

    Raises:
        FileNotFoundError: if the file does not exist.
        ValueError: if the YAML is malformed or fails Pydantic validation.
    """
    if not path.exists():
        raise FileNotFoundError(f"Manifest file not found: {path}")

    try:
        with path.open("r") as f:
            data = yaml.safe_load(f)
    except yaml.YAMLError as exc:
        raise ValueError(f"Failed to parse YAML from '{path}': {exc}") from exc

    if not isinstance(data, dict):
        raise ValueError(f"Manifest must be a YAML mapping, got {type(data).__name__}")

    try:
        return BundleManifest(**data)
    except ValidationError as exc:
        # Format Pydantic errors into a human-readable message
        lines = [f"Manifest validation failed for '{path}':"]
        for err in exc.errors():
            loc = " → ".join(str(part) for part in err["loc"])
            lines.append(f"  [{loc}] {err['msg']}")
        raise ValueError("\n".join(lines)) from exc
