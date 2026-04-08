from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import ValidationError

from offlinectl.manifest.schema import BundleManifest
from offlinectl.roles.expander import expand_roles


def load_manifest(path: Path) -> BundleManifest:
    """Load and validate a bundle.yaml manifest.

    If the manifest references roles, those are expanded (merged) into the
    task list before returning. Role tasks come first, then inline tasks.

    Raises:
        FileNotFoundError: if the file or a role path does not exist.
        ValueError: if the YAML is malformed, fails Pydantic validation,
                    or contains duplicate task names.
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
        manifest = BundleManifest(**data)
    except ValidationError as exc:
        # Format Pydantic errors into a human-readable message
        lines = [f"Manifest validation failed for '{path}':"]
        for err in exc.errors():
            loc = " → ".join(str(part) for part in err["loc"])
            lines.append(f"  [{loc}] {err['msg']}")
        raise ValueError("\n".join(lines)) from exc

    # Expand roles into the task list (no-op if spec.roles is empty)
    manifest_dir = path.parent.resolve()
    return expand_roles(manifest, manifest_dir)
