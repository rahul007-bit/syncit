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
from urllib.parse import urlparse

HOST_REPO_DIR = Path("/etc/yum.repos.d")
HOST_APT_SOURCES = Path("/etc/apt/sources.list")
HOST_APT_SOURCES_DIR = Path("/etc/apt/sources.list.d")

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
            # TLS material (e.g. RHEL CDN entitlement) — carried through to
            # browse and pack as --setopt=<repo>.ssl* options
            for key in ("sslcacert", "sslclientcert", "sslclientkey"):
                value = data.get(key)
                if value:
                    repo[key] = _first_line(value)
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


def _parse_apt_list_file(path: Path) -> list[tuple[str, str]]:
    """Parse a classic one-line-style .list file. Returns [(name_hint, source_line)]."""
    out: list[tuple[str, str]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if not stripped.startswith("deb "):
            continue  # skip deb-src
        # Rebuild without bracket options ([arch=...,signed-by=...]) to avoid
        # conflicts with the pack path (trusted=yes is injected by the plugin)
        tokens = stripped.split()
        if len(tokens) >= 4:
            _, url, suite, comps = tokens[0], tokens[1], tokens[2], tokens[3:]
            name_hint = urlparse(url).netloc
            out.append((name_hint, f"deb {url} {suite} {' '.join(comps)}"))
    return out


def _parse_apt_deb822_file(path: Path) -> list[tuple[str, str, str]]:
    """Parse a deb822-style .sources file. Returns [(name_hint, source_line, gpg_url)]."""
    out: list[tuple[str, str, str]] = []
    block: dict[str, str] = {}

    def flush() -> None:
        nonlocal block
        if not block:
            return
        try:
            enabled = block.get("Enabled", "yes").strip().lower() not in ("no", "false")
            types = block.get("Types", "")
            uris = block.get("URIs", "").split()
            suites = block.get("Suites", "").split()
            comps = block.get("Components", "").split()
            signed_by = block.get("Signed-By", "").strip()
        finally:
            block = {}
        if not enabled or "deb" not in types:
            return
        # Only http(s) Signed-By values are usable as gpg_key by the pack path;
        # local keyring paths are skipped (the pack path injects [trusted=yes])
        gpg = signed_by if signed_by.startswith(("http://", "https://")) else ""
        for uri in uris:
            for suite in suites:
                line = f"deb {uri} {suite} {' '.join(comps)}".strip()
                out.append((urlparse(uri).netloc, line, gpg))

    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.rstrip()
        if not line.strip():
            flush()
            continue
        if line.startswith("#"):
            continue
        if ":" in line and not line.startswith((" ", "\t")):
            key, _, value = line.partition(":")
            block[key.strip()] = value.strip()
    flush()
    return out


def filter_apt_sources_file(path: Path, covered: set[tuple[str, str]]) -> str:
    """
    Return the content of a deb822 .sources file with every (uri, suite) pair
    in `covered` removed (other suites in the same block are kept).
    """
    kept_blocks: list[str] = []
    block: dict[str, str] = {}
    order: list[str] = []

    def flush() -> None:
        nonlocal block, order
        if not order:
            block = {}
            return
        if block.get("Enabled", "yes").strip().lower() not in (
            "no",
            "false",
        ) and "deb" in block.get("Types", ""):
            uris = block.get("URIs", "").split()
            suites = block.get("Suites", "").split()
            remaining = [s for s in suites if not any((u, s) in covered for u in uris)]
            if remaining:
                out = []
                for key in order:
                    value = block[key]
                    if key == "Suites":
                        value = " ".join(remaining)
                    out.append(f"{key}: {value}")
                kept_blocks.append("\n".join(out))
        block = {}
        order = []

    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.rstrip()
        if not line.strip():
            flush()
            continue
        if line.startswith("#"):
            continue
        if ":" in line and not line.startswith((" ", "\t")):
            key, _, value = line.partition(":")
            key = key.strip()
            if key not in block:
                order.append(key)
            block[key] = value.strip()
    flush()
    return "\n\n".join(kept_blocks) + "\n" if kept_blocks else ""


def load_host_apt_repos(
    sources_list: Path | None = None,
    sources_dir: Path | None = None,
) -> list[dict[str, Any]]:
    """Parse the host's apt sources into catalog-shaped entries.

    Reads /etc/apt/sources.list, /etc/apt/sources.list.d/*.list and
    /etc/apt/sources.list.d/*.sources (deb822). Only http(s) Signed-By URLs
    are carried as gpg_key — local keyring paths are skipped since the pack
    path injects [trusted=yes] anyway.
    """
    main = sources_list if sources_list is not None else HOST_APT_SOURCES
    d = sources_dir if sources_dir is not None else HOST_APT_SOURCES_DIR

    entries: list[dict[str, Any]] = []
    seen: set[str] = set()

    def add(name_hint: str, line: str, origin: str, gpg_key: str = "") -> None:
        if line in seen:
            return
        seen.add(line)
        repo: dict[str, Any] = {"name": name_hint or origin, "url": line}
        if gpg_key:
            repo["gpg_key"] = gpg_key
        entries.append(
            {
                "id": f"{origin}-{len(entries)}",
                "label": f"[host] {name_hint or origin}",
                "description": f"{line} (from {origin})",
                "repo": repo,
                "repo_overrides": {},
                "vars": {},
            }
        )

    if main.is_file():
        for hint, line in _parse_apt_list_file(main):
            add(hint, line, main.name)
    if d.is_dir():
        for path in sorted(d.glob("*")):
            if not path.is_file():
                continue
            if path.suffix == ".list":
                for hint, line in _parse_apt_list_file(path):
                    add(hint, line, path.name)
            elif path.suffix == ".sources":
                for hint, line, gpg in _parse_apt_deb822_file(path):
                    add(hint, line, path.name, gpg)
    return entries
