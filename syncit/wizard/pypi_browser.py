"""Live PyPI package browser for the `syncit create` wizard.

Looks packages up via the PyPI JSON API (stdlib urllib — no new deps) and
resolves the full dependency closure with `pip install --dry-run --report`
(pip >= 23.0). Returns pinned `name==version` strings that the wizard writes
into a generated requirements.txt.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from typing import Any

import questionary
from rich import print as rprint


def fetch_package(name: str, timeout: int = 30) -> dict[str, Any] | None:
    """Fetch package metadata from PyPI. Returns None if not found."""
    url = f"https://pypi.org/pypi/{name}/json"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            data = json.loads(resp.read().decode())
    except (urllib.error.HTTPError, urllib.error.URLError, json.JSONDecodeError):
        return None
    versions = list(data.get("releases", {}).keys())

    # Sort newest-first using a numeric-aware key (best effort, stdlib only)
    def vkey(v: str) -> tuple:
        parts = []
        for chunk in v.replace("-", ".").split("."):
            digits = ""
            for ch in chunk:
                if ch.isdigit():
                    digits += ch
                else:
                    break
            parts.append((int(digits or 0), chunk))
        return tuple(parts)

    versions.sort(key=vkey, reverse=True)
    return {
        "name": data.get("info", {}).get("name", name),
        "summary": data.get("info", {}).get("summary", ""),
        "versions": versions,
    }


def resolve_pip_deps(spec: str, python_version: str = "") -> list[dict[str, str]]:
    """
    Resolve the full install closure for a requirement spec using pip's
    --dry-run --report. Returns [{'name': ..., 'version': ...}, ...].
    """
    from pathlib import Path

    base_cmd = [
        sys.executable,
        "-m",
        "pip",
        "install",
        "--dry-run",
        "--quiet",
        "--ignore-installed",
    ]
    variants: list[list[str]] = [[]]
    if python_version:
        # Resolving for a different interpreter requires binary-only wheels
        variants.insert(0, ["--python-version", python_version, "--only-binary=:all:"])

    with tempfile.TemporaryDirectory(prefix="syncit-pypi-") as tmp:
        report = Path(tmp) / "report.json"
        res = None
        for variant in variants:
            cmd = [*base_cmd, *variant, "--report", str(report), spec]
            try:
                res = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
            except subprocess.TimeoutExpired:
                rprint("[yellow]pip resolution timed out.[/yellow]")
                return []
            if res.returncode == 0 and report.exists():
                break
        if res is None or res.returncode != 0 or not report.exists():
            last = (res.stderr or res.stdout or "").strip().splitlines()
            rprint("[yellow]pip dependency resolution failed:[/yellow]")
            for line in last[-4:]:
                rprint(f"[dim]  {line}[/dim]")
            return []
        data = json.loads(report.read_text())
        out: list[dict[str, str]] = []
        for item in data.get("install", []):
            meta = item.get("metadata", {})
            name = meta.get("name")
            version = meta.get("version")
            if name and version:
                out.append({"name": name, "version": version})
        return out


def browse_pypi_packages() -> list[str]:
    """
    Interactive PyPI search/select loop. Returns a list of pinned
    'name==version' strings (empty list if cancelled / nothing selected).
    """
    selected: list[str] = []
    while True:
        name = questionary.text("PyPI package name (blank to finish):").ask()
        if not name:
            break
        name = name.strip()
        data = fetch_package(name)
        if data is None:
            rprint(f"[yellow]'{name}' not found on PyPI — check the spelling.[/yellow]")
            continue
        if data["summary"]:
            rprint(f"[dim]  {data['name']}: {data['summary'][:80]}[/dim]")
        versions = data["versions"][:40]
        version = questionary.select(
            f"Version for {data['name']}:",
            choices=[questionary.Choice(title=v, value=v) for v in versions],
        ).ask()
        if not version:
            continue
        pin = f"{data['name']}=={version}"
        if pin in selected:
            continue

        rprint("[cyan]Resolving dependency closure...[/cyan]")
        deps = resolve_pip_deps(pin)
        if deps:
            rprint(f"[cyan]Full closure: {len(deps)} package(s)[/cyan]")
            for d in deps[:25]:
                rprint(f"[dim]  {d['name']}=={d['version']}[/dim]")
            if len(deps) > 25:
                rprint(f"[dim]  ... and {len(deps) - 25} more[/dim]")
        else:
            rprint("[yellow]Could not resolve deps — pinning top-level package only.[/yellow]")
            selected.append(pin)
            continue

        if questionary.confirm(
            "Pin the FULL closure (all deps)? (No = top-level only)", default=True
        ).ask():
            selected.extend(f"{d['name']}=={d['version']}" for d in deps)
        else:
            selected.append(pin)
        rprint(f"[green]Added:[/] {pin}")

    return list(dict.fromkeys(selected))
