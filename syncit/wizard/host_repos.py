"""Discover the build host's own dnf repositories (/etc/yum.repos.d/*.repo).

These are offered in the wizard's repo picker next to the curated catalog, so
repos already configured on the machine (RHEL CDN via entitlement, installed
EPEL, PGDG, custom internal mirrors...) can be selected without re-entering
their URLs.
"""

from __future__ import annotations

import configparser
from pathlib import Path
from typing import Any

HOST_REPO_DIR = Path("/etc/yum.repos.d")

# Sections matching these suffixes are not useful for offline bundling
_SKIP_SUFFIXES = ("-source", "-debuginfo", "-debug", "-srpm", "-modular")


def _first_line(value: str | None) -> str:
    if not value:
        return ""
    return value.strip().splitlines()[0].strip()


def _is_enabled(data: configparser.SectionProxy) -> bool:
    enabled = str(data.get("enabled", fallback="1")).strip().lower()
    return enabled not in ("0", "false", "no")


def load_host_repos(repo_dir: Path | None = None) -> list[dict[str, Any]]:
    """Parse all enabled repos from *.repo files into catalog-shaped entries."""
    d = repo_dir if repo_dir is not None else HOST_REPO_DIR
    if not d.is_dir():
        return []

    entries: list[dict[str, Any]] = []
    seen: set[str] = set()
    for path in sorted(d.glob("*.repo")):
        parser = configparser.ConfigParser(strict=False, interpolation=None)
        try:
            parser.read(path)
        except configparser.Error:
            continue
        for section in parser.sections():
            if section in seen:
                continue
            if any(section.endswith(sfx) for sfx in _SKIP_SUFFIXES):
                continue
            data = parser[section]
            if not _is_enabled(data):
                continue
            baseurl = _first_line(data.get("baseurl"))
            mirror = _first_line(data.get("mirrorlist") or data.get("metalink"))
            url = baseurl or mirror
            if not url:
                continue
            gpgkey = _first_line(data.get("gpgkey"))
            repo: dict[str, Any] = {"name": section, "baseurl": url}
            if gpgkey:
                repo["gpgkey"] = gpgkey
            repo["gpgcheck"] = str(data.get("gpgcheck", fallback="1")).strip() == "1"
            if not baseurl and mirror:
                # mirrorlist URLs cannot be used as a dnf baseurl — surface
                # them but flag the limitation
                repo["_mirrorlist_only"] = True
            entries.append(
                {
                    "id": section,
                    "label": f"[host] {section}",
                    "description": f"{data.get('name', section).strip()} (from {path.name})",
                    "repo": repo,
                    "repo_overrides": {},
                    "vars": {},
                }
            )
            seen.add(section)
    return entries
