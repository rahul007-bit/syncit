"""Live PyPI package browser for the `syncit create` wizard.

Looks packages up via the PyPI JSON API (stdlib urllib — no new deps) and
resolves the full dependency closure with `pip install --dry-run --report`
(pip >= 23.0). Returns pinned `name==version` strings that the wizard writes
into a generated requirements.txt.
"""

from __future__ import annotations

import json
import re
import subprocess
import tempfile
import urllib.error
import urllib.request
from typing import Any

import questionary
from rich import print as rprint
from rich.markup import escape

from syncit.plugins.pip import pip_command_candidates

# "name", "name[extra1,extra2]" — extras are preserved for pip resolution
_SPEC_RE = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)\s*(\[[^\]]*\])?\s*$")


def _split_extras(spec: str) -> tuple[str, str]:
    """'patroni[etcd3]' -> ('patroni', '[etcd3]')."""
    m = _SPEC_RE.match(spec)
    if not m:
        return spec.strip(), ""
    return m.group(1), (m.group(2) or "")


def fetch_package(name: str, timeout: int = 30) -> dict[str, Any] | None:
    """Fetch package metadata from PyPI. Returns None if not found."""
    url = f"https://pypi.org/pypi/{name}/json"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            data = json.loads(resp.read().decode())
    except (urllib.error.HTTPError, urllib.error.URLError, json.JSONDecodeError):
        return None
    versions = list(data.get("releases", {}).keys())

    # Sort newest-first using a numeric-aware key (best effort, stdlib only).
    # Pre-release chunks (e.g. ".dev1", "0rc1") sort below the corresponding
    # stable numeric chunk.
    def vkey(v: str) -> tuple:
        parts = []
        for chunk in v.replace("-", ".").split("."):
            digits = ""
            for ch in chunk:
                if ch.isdigit():
                    digits += ch
                else:
                    break
            if digits:
                parts.append((1, int(digits), chunk))
            else:
                # non-numeric chunk: always sorts below numeric chunks
                parts.append((0, 0, chunk))
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

    variants: list[list[str]] = [[]]
    if python_version:
        # Resolving for a different interpreter requires binary-only wheels
        variants.insert(0, ["--python-version", python_version, "--only-binary=:all:"])

    last_err = ""
    with tempfile.TemporaryDirectory(prefix="syncit-pypi-") as tmp:
        report = Path(tmp) / "report.json"
        resolved = False
        for pip_prefix in pip_command_candidates():
            for variant in variants:
                report.unlink(missing_ok=True)
                cmd = [
                    *pip_prefix,
                    "install",
                    "--dry-run",
                    "--quiet",
                    "--ignore-installed",
                    *variant,
                    "--report",
                    str(report),
                    spec,
                ]
                try:
                    res = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
                except subprocess.TimeoutExpired:
                    rprint("[yellow]pip resolution timed out.[/yellow]")
                    return []
                except OSError as e:
                    last_err = str(e)
                    continue
                if res.returncode == 0 and report.exists():
                    resolved = True
                    break
                last_err = ((res.stderr or "") or (res.stdout or "")).strip()
            if resolved:
                break
        if not resolved:
            rprint("[yellow]pip dependency resolution failed:[/yellow]")
            for line in last_err.splitlines()[-4:]:
                rprint(f"[dim]  {escape(line)}[/dim]")
            rprint("[dim]  No usable pip found — install pip, or run syncit via 'uv run'.[/dim]")
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


def browse_pypi_packages(python_version: str = "") -> list[str] | None:
    """
    Interactive PyPI search/select loop. Returns a list of pinned
    'name==version' strings, or None when the user presses Ctrl+C.

    `python_version`: target interpreter for dependency resolution (e.g. "3.9"
    on RHEL 9); when set, pip resolves with --python-version/--only-binary.
    """
    selected: list[str] = []
    from syncit.wizard.history import prompt_search

    while True:
        name = prompt_search("pypi", "PyPI package name (Enter to search):", finish="Finish")
        if name is None:
            return None
        if not name or not name.strip():
            break
        base_name, extras = _split_extras(name.strip())
        data = fetch_package(base_name)
        if data is None:
            rprint(
                f"[yellow]'{escape(name.strip())}' not found on PyPI — check the spelling.[/yellow]"
            )
            continue
        if data["summary"]:
            rprint(f"[dim]  {escape(data['name'])}: {escape(data['summary'][:80])}[/dim]")
        versions = data["versions"][:40]
        version = questionary.select(
            f"Version for {data['name']}:",
            choices=[questionary.Choice(title=v, value=v) for v in versions],
        ).ask()
        if not version:
            continue
        pin = f"{data['name']}{extras}=={version}"
        if pin in selected:
            continue

        rprint("[cyan]Resolving dependency closure...[/cyan]")
        deps = resolve_pip_deps(pin, python_version=python_version)
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
        rprint(f"[green]Added:[/] {escape(pin)}")

    return list(dict.fromkeys(selected))
