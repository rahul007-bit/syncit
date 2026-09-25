"""Live DNF package browser for the `syncit create` wizard.

Runs `dnf repoquery` against the wizard-selected upstream repos using
user-space caches (--setopt=cachedir) and never mutates system repo config.
Returns fully-pinned nevra strings (name-evr.arch) suitable for both
`dnf download --resolve` (pack) and `dnf install` (apply).
"""

from __future__ import annotations

import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

import questionary
from rich import print as rprint

WIZARD_CACHE_DIR = Path("~/.cache/syncit/wizard").expanduser()

# Matches RPM nevra output (name-version-release.arch). Used to filter
# subscription-manager / dnf plugin noise lines out of repoquery output.
NEVRA_RE = re.compile(
    r"^[A-Za-z0-9][\w+.-]*-[^\s-]+-[^\s-]+\.(x86_64|noarch|aarch64|i686|s390x|ppc64le|riscv64)$"
)


def _is_nevra(line: str) -> bool:
    return bool(NEVRA_RE.match(line))


def repo_id(repo_name: str, prefix: str = "syncit") -> str:
    return prefix + "_" + re.sub(r"[^a-zA-Z0-9._-]", "_", repo_name)


def build_repo_opts(repos: list[dict], prefix: str = "syncit") -> list[str]:
    """Build --repofrompath/--enablerepo options from manifest repo dicts."""
    opts: list[str] = []
    for repo in repos:
        name = repo_id(repo.get("name", "repo"), prefix)
        baseurl = repo.get("baseurl", "")
        if not baseurl:
            continue
        opts.extend(["--repofrompath", f"{name},{baseurl}", "--enablerepo", name])
        opts.extend(["--setopt", f"{name}.gpgcheck=0"])
        # A dead/unreachable repo must not break the whole query
        opts.extend(["--setopt", f"{name}.skip_if_unavailable=true"])
    return opts


def _base_args(releasever: str, basearch: str, cachedir: Path = WIZARD_CACHE_DIR) -> list[str]:
    args = [
        "--disablerepo=*",
        "--setopt=cachedir=" + str(cachedir),
        "--arch",
        f"{basearch},noarch",
    ]
    if releasever:
        args.extend(["--releasever", releasever])
    return args


def _run(cmd: list[str], timeout: int = 900) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def _run_streaming(cmd: list[str], timeout: int = 900) -> int:
    """Run a command streaming output live (for progress visibility)."""
    process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    assert process.stdout is not None
    try:
        for line in iter(process.stdout.readline, ""):
            rprint(f"[dim]  {line.rstrip()}[/dim]")
        return process.wait(timeout=timeout)
    finally:
        if process.stdout:
            process.stdout.close()


def warm_metadata(repos: list[dict], releasever: str, basearch: str) -> bool:
    """
    Download repo metadata into the wizard cache with live progress.
    Returns True if makecache succeeded (or was already cached).
    """
    # makecache does not accept --arch (nor --releasever-only semantics);
    # build a minimal arg set: disablerepo + cachedir + injected repos.
    args = ["--disablerepo=*", "--setopt=cachedir=" + str(WIZARD_CACHE_DIR)]
    if releasever:
        args.extend(["--releasever", releasever])
    cmd = ["dnf", "makecache", "-y", *args, *build_repo_opts(repos)]
    rprint(
        "[cyan]Downloading repo metadata (first run per repo set — later searches are instant)...[/cyan]"
    )
    try:
        rc = _run_streaming(cmd)
    except subprocess.TimeoutExpired:
        rprint("[yellow]Metadata download timed out — continuing with partial cache.[/yellow]")
        return False
    if rc != 0:
        rprint(
            "[yellow]Metadata fetch had errors — some repos may be unavailable. Trying anyway.[/yellow]"
        )
        return False
    rprint("[green]Repo metadata ready.[/green]")
    return True


def _fail(res: subprocess.CompletedProcess, context: str) -> None:
    rprint(f"[red]dnf {context} failed:[/red]")
    output = (res.stderr or res.stdout or "").strip().splitlines()
    for line in output[-6:]:
        rprint(f"[dim]  {line}[/dim]")


def search_packages(
    repos: list[dict], releasever: str, basearch: str, term: str, limit: int = 300
) -> list[tuple[str, str]]:
    """Search packages across the wizard repos. Returns [(name, summary)]."""
    term = term.strip()
    rprint(f"[cyan]Searching {len(repos)} repo(s) for '{term}'... (Ctrl-C to cancel)[/cyan]")
    t0 = time.time()
    cmd = [
        "dnf",
        "repoquery",
        *_base_args(releasever, basearch),
        *build_repo_opts(repos),
        "--qf",
        "%{name}|%{summary}",
        f"{term}*",
    ]
    res = _run(cmd, timeout=600)
    elapsed = time.time() - t0
    if res.returncode != 0:
        _fail(res, f"repoquery '{term}' ({elapsed:.1f}s)")
        return []
    seen: set[str] = set()
    out: list[tuple[str, str]] = []
    for line in res.stdout.splitlines():
        if "|" not in line:
            continue
        name, summary = line.split("|", 1)
        name = name.strip()
        if not name or name in seen:
            continue
        seen.add(name)
        out.append((name, summary.strip()))
        if len(out) >= limit:
            rprint(
                f"[yellow]Result list truncated to first {limit} matches — refine your search for more.[/yellow]"
            )
            break
    if not out:
        # rc=0 but no matches: show what dnf actually said so a silent
        # metadata re-download or repo error is visible
        detail = (res.stderr or "").strip().splitlines()
        rprint(
            f"[yellow]dnf returned 0 matches in {elapsed:.1f}s."
            + (
                " Metadata may still be downloading or repos may have failed:"
                + " | ".join(detail[-2:])
                if detail
                else ""
            )
            + "[/yellow]"
        )
    else:
        rprint(f"[green]Found {len(out)} match(es) in {elapsed:.1f}s[/green]")
    return out


def list_versions(
    repos: list[dict], releasever: str, basearch: str, name: str, limit: int = 60
) -> list[str]:
    """List available versions for a package, newest first. Returns ['name-evr.arch', ...]."""
    cmd = [
        "dnf",
        "repoquery",
        "--showduplicates",
        *_base_args(releasever, basearch),
        *build_repo_opts(repos),
        "--qf",
        "%{name}-%{evr}.%{arch}",
        name,
    ]
    res = _run(cmd, timeout=600)
    if res.returncode != 0:
        _fail(res, f"versions for '{name}'")
        return []
    # dnf prints oldest→newest; reverse so newest versions are offered first
    nevras = [n.strip() for n in res.stdout.splitlines() if _is_nevra(n.strip())]
    return list(dict.fromkeys(reversed(nevras)))[:limit]


def resolve_deps(
    repos: list[dict],
    releasever: str,
    basearch: str,
    nevras: list[str],
    installroot: str | None = None,
) -> list[tuple[str, int]]:
    """Resolve transitive deps for nevras. Returns [(name-evr.arch, size_bytes)]."""
    cmd = [
        "dnf",
        "repoquery",
        "--requires",
        "--resolve",
        *_base_args(releasever, basearch),
        *build_repo_opts(repos),
        "--qf",
        "%{name}-%{evr}.%{arch}|%{size}",
    ]
    if installroot:
        cmd.extend(["--installroot", installroot])
    cmd.extend(nevras)
    res = _run(cmd, timeout=900)
    if res.returncode != 0:
        last = res.stderr.strip().splitlines()[-1] if res.stderr.strip() else "unknown error"
        rprint(f"[yellow]Dependency resolution failed: {last}[/yellow]")
        return []
    out: list[tuple[str, int]] = []
    for line in res.stdout.splitlines():
        if "|" not in line:
            continue
        nevra, size = line.rsplit("|", 1)
        nevra = nevra.strip()
        if not _is_nevra(nevra):
            continue
        try:
            out.append((nevra, int(size)))
        except ValueError:
            out.append((nevra, 0))
    return out


def _human_size(n: int) -> str:
    size = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024:
            return f"{size:.0f} {unit}" if unit != "B" else f"{size:.0f} B"
        size /= 1024
    return f"{size:.1f} TB"


def browse_dnf_packages(
    repos: list[dict],
    releasever: str,
    basearch: str,
    installroot: str | None = None,
) -> list[str]:
    """
    Interactive search/select loop. Returns a list of pinned nevra strings,
    or [] if the user cancels / nothing selected.
    """
    if not shutil.which("dnf"):
        rprint(
            "[yellow]dnf not available on this machine — skipping live package browsing.[/yellow]"
        )
        return []
    if not repos:
        rprint(
            "[yellow]No upstream repos selected — live browsing needs at least one repo (system repos are disabled in the wizard).[/yellow]"
        )
        return []

    warm_metadata(repos, releasever, basearch)
    rprint("[cyan]Type a package name prefix to search (blank to finish).[/cyan]")
    selected: list[str] = []

    while True:
        term = questionary.text("Search packages (name prefix, blank to finish):").ask()
        if not term or not term.strip():
            break
        matches = search_packages(repos, releasever, basearch, term)
        if not matches:
            rprint("[yellow]No matches. Try a shorter prefix.[/yellow]")
            continue
        choices = [
            questionary.Choice(title=f"{n:<38} [dim]{s[:60]}[/dim]", value=n) for n, s in matches
        ]
        picked = (
            questionary.checkbox(
                "Select packages (space to toggle, Enter to confirm):",
                choices=choices,
            ).ask()
            or []
        )
        for name in picked:
            if name in selected:
                continue
            versions = list_versions(repos, releasever, basearch, name)
            if not versions:
                rprint(f"[yellow]Skipping {name}: no versions found.[/yellow]")
                continue
            nevra = questionary.select(
                f"Version for {name}:",
                choices=[questionary.Choice(title=v, value=v) for v in versions],
            ).ask()
            if not nevra:
                continue
            selected.append(nevra)
            rprint(f"[green]Added:[/] {nevra}")

    if not selected:
        return []

    action = questionary.select(
        "Package selection:",
        choices=[
            "Review / remove packages",
            "Pin full dependency closure (recommended)",
            "Use as-is",
            "Cancel browsing",
        ],
    ).ask()
    if action is None or action == "Cancel browsing":
        return []
    if action == "Review / remove packages":
        keep = (
            questionary.checkbox(
                "Keep these packages (uncheck to remove):",
                choices=[questionary.Choice(title=s, value=s, checked=True) for s in selected],
            ).ask()
            or []
        )
        selected = [s for s in selected if s in keep]

    if action in ("Pin full dependency closure (recommended)", "Review / remove packages"):
        rprint("[cyan]Resolving transitive dependencies...[/cyan]")
        deps = resolve_deps(repos, releasever, basearch, selected, installroot=installroot)
        total = sum(size for _, size in deps)
        rprint(f"[cyan]Total resolved: {len(deps)} package(s), {_human_size(total)}[/cyan]")
        for nevra, size in deps[:40]:
            rprint(f"[dim]  {nevra:<45} {_human_size(size)}[/dim]")
        if len(deps) > 40:
            rprint(f"[dim]  ... and {len(deps) - 40} more[/dim]")
        if (
            deps
            and questionary.confirm(
                "Pin all resolved dependencies into the manifest?", default=True
            ).ask()
        ):
            top_names = {s.rsplit("-", 2)[0] for s in selected}
            pinned = list(selected)
            for dep, _ in deps:
                if dep.rsplit("-", 2)[0] not in top_names and dep not in pinned:
                    pinned.append(dep)
            return pinned
    return selected
