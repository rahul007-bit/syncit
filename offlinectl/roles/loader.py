"""Load and validate a role.yaml from a role directory."""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import ValidationError

from offlinectl.roles.schema import Role


def load_role(path: Path) -> Role:
    """Load and validate a role from a role directory.

    Args:
        path: Path to the role directory (containing role.yaml).

    Returns:
        A validated Role instance.

    Raises:
        FileNotFoundError: if the role path or role.yaml does not exist.
        ValueError: if role.yaml is malformed or fails Pydantic validation.
    """
    if not path.exists():
        raise FileNotFoundError(f"Role path '{path}' does not exist.")

    role_file = path / "role.yaml"
    if not role_file.exists():
        raise FileNotFoundError(f"role.yaml not found in role path '{path}'.")

    try:
        with role_file.open("r") as f:
            data = yaml.safe_load(f)
    except yaml.YAMLError as exc:
        raise ValueError(f"Failed to parse role.yaml in '{path}': {exc}") from exc

    if not isinstance(data, dict):
        raise ValueError(f"role.yaml in '{path}' must be a YAML mapping, got {type(data).__name__}")

    try:
        return Role(**data)
    except ValidationError as exc:
        lines = [f"Role validation failed for '{role_file}':"]
        for err in exc.errors():
            loc = " → ".join(str(part) for part in err["loc"])
            lines.append(f"  [{loc}] {err['msg']}")
        raise ValueError("\n".join(lines)) from exc
