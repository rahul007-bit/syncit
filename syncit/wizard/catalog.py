"""Curated upstream repo catalog for the `syncit create` wizard.

Data lives in `syncit/data/repos/<family>.yaml` (el = RHEL/Rocky/Alma/CentOS).
Entries use DNF's native `$releasever`/`$basearch` variables, which the wizard
substitutes with the target's values at manifest-generation time so packs are
deterministic regardless of the build host's own distro.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

REPO_DATA_DIR = Path(__file__).resolve().parent.parent / "data" / "repos"

# distro id (os-release ID) -> catalog family file
_DISTRO_TO_FILE: dict[str, str] = {
    "rhel": "el",
    "rocky": "el",
    "almalinux": "el",
    "centos": "el",
    "fedora": "fedora",
}

# manifest arch (amd64/arm64) -> RPM basearch
ARCH_TO_BASEARCH = {"amd64": "x86_64", "arm64": "aarch64", "x86_64": "x86_64", "aarch64": "aarch64"}


def supported_distros() -> list[str]:
    return sorted(_DISTRO_TO_FILE)


def load_repos(distro_id: str) -> list[dict[str, Any]]:
    """Load curated repo entries that support `distro_id` (e.g. 'rocky')."""
    family = _DISTRO_TO_FILE.get(distro_id)
    if not family:
        return []
    path = REPO_DATA_DIR / f"{family}.yaml"
    if not path.exists():
        return []
    try:
        entries = yaml.safe_load(path.read_text()) or []
    except yaml.YAMLError as exc:
        return []

    result: list[dict[str, Any]] = []
    for entry in entries:
        if not isinstance(entry, dict) or "id" not in entry or "repo" not in entry:
            continue
        if distro_id not in entry.get("distros", []):
            continue
        result.append(
            {
                "id": entry["id"],
                "label": entry.get("label", entry["id"]),
                "description": entry.get("description", ""),
                "repo": entry["repo"],
                "repo_overrides": entry.get("repo_overrides") or {},
                "vars": entry.get("vars") or {},
            }
        )
    return result


def substitute_vars(text: str, values: dict[str, str]) -> str:
    """Replace `{name}` and `$name` placeholders with `values[name]`."""
    out = text
    for name, value in values.items():
        out = out.replace("{" + name + "}", value)
        out = out.replace("$" + name, value)
    return out


def render_repo(
    entry: dict[str, Any],
    distro_id: str,
    releasever: str = "",
    basearch: str = "x86_64",
    values: dict[str, str] | None = None,
) -> dict[str, Any]:
    """
    Resolve a catalog entry for a distro into a dnf repo config dict
    ({name, baseurl, gpgkey?, gpgcheck?}) with all variables substituted.
    """
    repo = dict(entry["repo"])
    override = (entry.get("repo_overrides") or {}).get(distro_id) or {}
    repo.update(override)

    subs: dict[str, str] = {
        "releasever": releasever,
        "basearch": basearch,
        "distro": distro_id,
        **(values or {}),
    }
    rendered = {k: substitute_vars(v, subs) if isinstance(v, str) else v for k, v in repo.items()}
    return rendered
