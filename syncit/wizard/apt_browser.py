"""Live apt package browser for the `syncit create` wizard.

Builds a user-space apt environment (temp sources dir + ~/.cache apt state —
same technique as AptPlugin.pack) and runs `apt-cache search`, `apt-cache
madison`, and `apt-get install --print-uris` against it. Never mutates system
repo config. Returns "name=version" pins that pack downloads and installs.
"""

from __future__ import annotations

import atexit
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Callable

import questionary
from rich import print as rprint

from syncit.wizard.ranking import rank_matches

APT_CACHE_ROOT = Path("~/.cache/syncit/wizard-apt").expanduser()

# Matches "<name>_<version>_<arch>.deb" filenames from --print-uris output
_DEB_FILENAME_RE = re.compile(r"^([^_]+)_([^_]+)_[^.]+\.deb$")

# Session-scoped temp sources dir — created on first warm, reused by every
# apt-cache/apt-get call in this process, cleaned up at exit.
_SESSION: dict[str, Any] = {}
atexit.register(
    lambda: (
        shutil.rmtree(_SESSION["sources"], ignore_errors=True) if _SESSION.get("sources") else None
    )
)


def _session_sources(repos: list[dict]) -> Path:
    """Return (creating on first use) this session's temp sources dir."""
    if not _SESSION.get("sources"):
        _SESSION["sources"] = _build_temp_sources(repos)
    return _SESSION["sources"]


def _inject_trusted(source_line: str, signed_by: str | None = None) -> str:
    """Inject trusted=yes (and optional signed-by keyring) into an apt source line."""
    opts = ["trusted=yes"]
    if signed_by:
        opts.append(f"signed-by={signed_by}")
    joined = " ".join(opts)
    if source_line.strip().startswith("deb ["):
        return source_line.replace("deb [", f"deb [{joined} ", 1)
    return source_line.replace("deb ", f"deb [{joined}] ", 1)


def _fetch_gpg_key(gpg_key_url: str, dest: Path) -> Path | None:
    """Download and dearmor a repo GPG key for signed-by use. Returns path or None."""
    import urllib.request

    try:
        if not gpg_key_url.startswith(("http://", "https://")):
            return None
        urllib.request.urlretrieve(gpg_key_url, str(dest))
        raw = dest.read_bytes()
        if raw.startswith(b"-----BEGIN PGP PUBLIC KEY BLOCK-----"):
            armored = dest.with_suffix(".asc")
            dest.rename(armored)
            subprocess.run(
                ["gpg", "--dearmor", "--yes", "-o", str(dest), str(armored)],
                capture_output=True,
                timeout=60,
            )
            armored.unlink(missing_ok=True)
        return dest if dest.is_file() else None
    except Exception as exc:
        rprint(f"[yellow]GPG key fetch failed ({gpg_key_url[:60]}): {exc}[/yellow]")
        return None


def _uri_suite(source_line: str) -> tuple[str, str]:
    parts = source_line.split()
    if len(parts) >= 3:
        return (parts[1], parts[2])
    return (source_line, "")


def _host_covered_pairs() -> set[tuple[str, str]]:
    """(uri, suite) pairs already configured on the host — wizard duplicates
    of these would conflict on the Trusted option."""
    from syncit.wizard.host_repos import _parse_apt_deb822_file, _parse_apt_list_file

    pairs: set[tuple[str, str]] = set()
    files: list[Path] = []
    main = Path("/etc/apt/sources.list")
    if main.is_file():
        files.append(main)
    sys_d = Path("/etc/apt/sources.list.d")
    if sys_d.is_dir():
        files.extend(sorted(sys_d.glob("*.list")))
        files.extend(sorted(sys_d.glob("*.sources")))
    for f in files:
        if f.suffix == ".sources":
            for _, line, _ in _parse_apt_deb822_file(f):
                pairs.add(_uri_suite(line))
        else:
            for _, line in _parse_apt_list_file(f):
                pairs.add(_uri_suite(line))
    return pairs


def _build_temp_sources(repos: list[dict], with_host_sources: bool = True) -> Path:
    """Create a temp apt sources dir: selected wizard repos (+ host system sources)."""
    temp = Path(tempfile.mkdtemp(prefix="syncit-apt-wizard-"))
    keys_dir = temp / "keys"
    keys_dir.mkdir(parents=True, exist_ok=True)
    (temp / "sources.list.d").mkdir(parents=True, exist_ok=True)

    main = Path("/etc/apt/sources.list")
    if with_host_sources and main.is_file():
        (temp / "sources.list").write_text(main.read_text(encoding="utf-8"))

    covered = _host_covered_pairs() if with_host_sources else set()
    repo_lines: list[str] = []
    seen_pairs: set[tuple[str, str]] = set(covered)
    for repo in repos:
        url = repo.get("url", "")
        if not url:
            continue
        pair = _uri_suite(url)
        if pair in seen_pairs:
            rprint(
                f"[dim]  {repo.get('name', '?')}: already provided by host sources — not duplicating[/dim]"
            )
            continue
        seen_pairs.add(pair)
        signed_by = None
        gpg_key = repo.get("gpg_key")
        if gpg_key:
            safe = re.sub(r"[^a-zA-Z0-9._-]", "_", repo.get("name", "repo"))
            key_path = _fetch_gpg_key(gpg_key, keys_dir / f"{safe}.gpg")
            if key_path:
                signed_by = str(key_path)
        repo_lines.append(_inject_trusted(url, signed_by))
    if repo_lines:
        (temp / "sources.list.d" / "syncit-wizard.list").write_text(
            "\n".join(repo_lines) + "\n", encoding="utf-8"
        )

    sys_d = Path("/etc/apt/sources.list.d")
    if with_host_sources and sys_d.is_dir():
        for f in sorted(sys_d.glob("*")):
            if f.is_file() and f.suffix in (".list", ".sources"):
                try:
                    (temp / "sources.list.d" / f.name).write_text(f.read_text(encoding="utf-8"))
                except OSError:
                    pass
    return temp


def _base_opts(cache_root: Path) -> list[str]:
    """User-space apt options (mirrors AptPlugin.pack)."""
    state_dir = cache_root / "state"
    cache_dir = cache_root / "cache"
    state_dir.mkdir(parents=True, exist_ok=True)
    cache_dir.mkdir(parents=True, exist_ok=True)
    return [
        "-o",
        f"Dir::State={state_dir}",
        "-o",
        f"Dir::Cache={cache_dir}",
    ]


def _status_opts(status_file: str) -> list[str]:
    """Resolve against a foreign dpkg status (base_installroot) instead of the host."""
    return [
        "-o",
        f"Dir::State::status={status_file}",
        # Disable binary caches so apt reads the custom status file fresh
        "-o",
        "Dir::Cache::pkgcache=",
        "-o",
        "Dir::Cache::srcpkgcache=",
    ]


def _source_opts(temp_sources: Path) -> list[str]:
    opts = []
    if (temp_sources / "sources.list").exists():
        opts.extend(["-o", f"Dir::Etc::SourceList={temp_sources / 'sources.list'}"])
    opts.extend(["-o", f"Dir::Etc::SourceParts={temp_sources / 'sources.list.d'}"])
    return opts


def _run_streaming(cmd: list[str], timeout: int = 600) -> int:
    process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    assert process.stdout is not None
    try:
        for line in iter(process.stdout.readline, ""):
            rprint(f"[dim]  {line.rstrip()}[/dim]")
        return process.wait(timeout=timeout)
    finally:
        if process.stdout:
            process.stdout.close()


def warm_apt_metadata(repos: list[dict], installroot: str | None = None) -> bool:
    """Refresh user-space apt metadata for the selected repos with live progress."""
    if not shutil.which("apt-get"):
        return False
    temp = _session_sources(repos)
    status = None
    if installroot:
        sf = Path(installroot) / "var" / "lib" / "dpkg" / "status"
        if sf.is_file():
            status = str(sf)
    cmd = ["apt-get", "update", *_base_opts(APT_CACHE_ROOT), *_source_opts(temp)]
    if status:
        cmd.extend(_status_opts(status))
    rprint(
        "[cyan]Downloading apt metadata (first run per repo set — later searches are instant)...[/cyan]"
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
    rprint("[green]Apt metadata ready.[/green]")
    return True


def search_packages(term: str, sort_mode: str = "relevance") -> list[tuple[str, str]]:
    """Search packages across wizard repos + host sources. Returns [(name, summary)]."""
    term = term.strip()
    rprint(f"[cyan]Searching apt for '{term}'... (Ctrl-C to cancel)[/cyan]")
    cmd = [
        "apt-cache",
        "search",
        term,
        *_base_opts(APT_CACHE_ROOT),
        *_source_opts(_session_sources([])),
    ]
    res = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    out: list[tuple[str, str]] = []
    for line in res.stdout.splitlines():
        if " - " not in line:
            continue
        name, summary = line.split(" - ", 1)
        name = name.strip()
        if name and not any(n == name for n, _ in out):
            out.append((name, summary.strip()))
    out = rank_matches(term, out)
    if sort_mode == "name":
        out = sorted(out)
    if out:
        rprint(f"[green]Found {len(out)} match(es)[/green]")
    else:
        rprint("[yellow]No matches. Try a shorter prefix.[/yellow]")
    return out


def list_versions(name: str) -> list[str]:
    """List available versions for a package via apt-cache madison (newest first)."""
    from syncit.wizard.rpm_browser import _vkey

    res = subprocess.run(
        [
            "apt-cache",
            "madison",
            name,
            *_base_opts(APT_CACHE_ROOT),
            *_source_opts(_session_sources([])),
        ],
        capture_output=True,
        text=True,
        timeout=120,
    )
    versions: list[str] = []
    for line in res.stdout.splitlines():
        parts = [p.strip() for p in line.split("|")]
        if len(parts) >= 2 and parts[1] and parts[1] not in versions:
            versions.append(parts[1])
    versions.sort(key=_vkey, reverse=True)
    return versions


def resolve_download_set(
    pins: list[str], installroot: str | None = None
) -> tuple[list[str], int, str | None]:
    """
    Run the pack-identical solver: apt-get install --print-uris against the
    wizard's metadata cache. Returns (pkg=version pins, total_size_bytes, error).
    apt's solver is transactional, so this set is co-installable by construction.
    """
    if not pins:
        return [], 0, None
    cmd = [
        "apt-get",
        "install",
        "--print-uris",
        "-qq",
        "--no-install-recommends",
        "-y",
        *_base_opts(APT_CACHE_ROOT),
        *_source_opts(_session_sources([])),
    ]
    if installroot:
        sf = Path(installroot) / "var" / "lib" / "dpkg" / "status"
        if sf.is_file():
            cmd.extend(_status_opts(str(sf)))
    cmd.extend(pins)
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    except subprocess.TimeoutExpired:
        return [], 0, "apt-get resolution timed out"
    if res.returncode != 0:
        err = (res.stderr or res.stdout or "").strip().splitlines()
        return [], 0, "\n".join(err[-6:] or ["unknown error"])
    targets: list[str] = []
    total = 0
    for line in res.stdout.splitlines():
        line = line.strip()
        if not line.startswith("'"):
            continue
        parts = line.split()
        if len(parts) < 3:
            continue
        filename = parts[1]
        m = _DEB_FILENAME_RE.match(filename)
        if m:
            pkg_name = m.group(1)
            version = m.group(2).replace("%3a", ":")
            targets.append(f"{pkg_name}={version}")
        else:
            targets.append(filename.split("_", 1)[0])
        try:
            total += int(parts[2])
        except ValueError:
            pass
    return sorted(set(targets)), total, None


def _human_size(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f} {unit}" if unit != "B" else f"{n:.0f} B"
        n /= 1024
    return f"{n:.1f} TB"


def browse_apt_packages(
    repos: list[dict],
    codename: str = "",
    installroot: str | None = None,
    add_repos: Callable[[], list[dict] | None] | None = None,
    initial: list[str] | None = None,
) -> list[str] | None:
    """
    Interactive search/select loop. Returns "name=version" pin strings
    ([] if finished with nothing selected, None if the user explicitly
    cancels / Ctrl+C). `initial` preloads an existing selection (edit flow).
    """
    if not shutil.which("apt-get"):
        rprint(
            "[yellow]apt-get not available on this machine — skipping live package browsing.[/yellow]"
        )
        return []
    if not repos and not add_repos:
        rprint(
            "[yellow]No upstream repos selected — live browsing needs at least one repo (or the host's own sources).[/yellow]"
        )
        return []

    warm_apt_metadata(repos, installroot)
    rprint(
        "[cyan]Search packages, add repos, or finish — your selection is kept between steps.[/cyan]"
    )
    selected: list[str] = list(initial or [])  # "name=version" pins
    sort_mode = "relevance"

    def _search_round() -> bool:
        from syncit.wizard.history import prompt_search

        while True:
            term = prompt_search(
                "apt", "Search packages (type name, press Enter):", finish="Finish"
            )
            if term is None:
                return True
            if not term or not term.strip():
                return False
            matches = search_packages(term, sort_mode)
            if not matches:
                continue
            choices = [questionary.Choice(title=f"{n:<38}  {s[:60]}", value=n) for n, s in matches]
            picked = (
                questionary.checkbox(
                    "Select packages (space to toggle, Enter to confirm):",
                    choices=choices,
                    use_search_filter=True,
                    use_jk_keys=False,
                ).ask()
                or []
            )
            if not picked:
                rprint(
                    "[yellow]Nothing selected — press <space> to toggle items, then Enter to confirm.[/yellow]"
                )
            for name in picked:
                versions = list_versions(name)
                if not versions:
                    rprint(f"[yellow]Skipping {name}: no versions found.[/yellow]")
                    continue
                version = questionary.select(
                    f"Version for {name}:",
                    choices=[questionary.Choice(title=v, value=v) for v in versions[:40]],
                ).ask()
                if not version:
                    continue
                pin = f"{name}={version}"
                if pin not in selected:
                    selected.append(pin)
                    rprint(f"[green]Added:[/] {pin}")

    def _pin_closure() -> list[str] | None:
        if not selected:
            return None
        rprint("[cyan]Resolving transitive dependencies (pack-identical solver)...[/cyan]")
        download_set, total, err = resolve_download_set(selected, installroot=installroot)
        if err is not None:
            rprint("[red]apt dependency resolution failed:[/red]")
            for line in err.splitlines():
                rprint(f"[yellow]  {line}[/yellow]")
            return None
        rprint(f"[cyan]Total resolved: {len(download_set)} package(s), {_human_size(total)}[/cyan]")
        for p in download_set[:40]:
            rprint(f"[dim]  {p:<48}[/dim]")
        if len(download_set) > 40:
            rprint(f"[dim]  ... and {len(download_set) - 40} more[/dim]")
        if not questionary.confirm(
            "Pin the full dependency closure into the manifest?", default=True
        ).ask():
            return None
        return download_set

    if selected:
        rprint(f"[cyan]Loaded {len(selected)} existing selection(s) — review or add more.[/cyan]")
    else:
        _search_round()
    while True:
        if not selected:
            if _search_round():
                return None
            if not selected:
                return []
        choices: list[str] = []
        if add_repos:
            choices.append("Add more upstream repos")
        choices += [
            "Search packages",
            "Review / remove packages",
            "Pin full dependency closure (recommended)",
            f"Change sorting (current: {sort_mode})",
            "Use as-is",
            "Cancel browsing",
        ]
        action = questionary.select("Package selection:", choices=choices).ask()
        if action is None or action == "Cancel browsing":
            return None
        if action == "Use as-is":
            return selected
        if action.startswith("Change sorting"):
            sort_mode = "name" if sort_mode == "relevance" else "relevance"
            rprint(f"[cyan]Search results will be sorted by: {sort_mode}[/cyan]")
            continue
        if action == "Add more upstream repos":
            added = add_repos() if add_repos else []
            known = {r.get("name") for r in repos}
            new = [r for r in (added or []) if r.get("name") not in known]
            if new:
                repos.extend(new)
                warm_apt_metadata(new, installroot)
                rprint(f"[green]Repo(s) added:[/] {', '.join(r['name'] for r in new)}")
            continue
        if action == "Search packages":
            _search_round()
            continue
        if action == "Review / remove packages":
            keep = (
                questionary.checkbox(
                    "Keep these packages (uncheck to remove):",
                    choices=[questionary.Choice(title=s, value=s, checked=True) for s in selected],
                ).ask()
                or []
            )
            selected = [s for s in selected if s in keep]
            continue
        result = _pin_closure()
        if result is not None:
            return result
