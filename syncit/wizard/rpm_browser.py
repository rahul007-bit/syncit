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

from syncit.wizard.ranking import rank_matches

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
        # TLS material (e.g. RHEL CDN entitlement certs from host repos)
        for opt_key in ("sslcacert", "sslclientcert", "sslclientkey"):
            value = repo.get(opt_key)
            if value:
                opts.extend(["--setopt", f"{name}.{opt_key}={value}"])
    return opts


def check_repo_url(
    baseurl: str,
    releasever: str = "",
    basearch: str = "x86_64",
    sslcacert: str | None = None,
    sslclientcert: str | None = None,
    sslclientkey: str | None = None,
    timeout: int = 25,
) -> bool:
    """
    Probe a repo by fetching its repomd.xml. Returns True if reachable (2xx).
    Uses curl (same TLS behavior as dnf); entitlement certs are passed when
    provided so CDN repos probe correctly. GET (not HEAD) — some CDNs
    (cdn.redhat.com) reject HEAD requests. dnf variables in the URL are
    expanded before probing (curl does not understand them).
    """
    expanded = (
        baseurl.replace("$releasever", releasever or "$releasever")
        .replace("$basearch", basearch or "$basearch")
        .replace("$base", releasever or "$base")
    )
    repomd = expanded.rstrip("/") + "/repodata/repomd.xml"
    cmd = ["curl", "-sL", "-m", str(timeout), "-o", "/dev/null", "-w", "%{http_code}"]
    if sslcacert:
        cmd.extend(["--cacert", sslcacert])
    if sslclientcert and sslclientkey:
        cmd.extend(["--cert", sslclientcert, "--key", sslclientkey])
    cmd.append(repomd)
    try:
        res = _run(cmd, timeout=timeout + 10)
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return False
    codes = [line.strip() for line in (res.stdout or "").splitlines() if line.strip()]
    return bool(codes) and codes[-1].startswith("2")


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
    repos: list[dict],
    releasever: str,
    basearch: str,
    term: str,
    sort_mode: str = "relevance",
) -> list[tuple[str, str]]:
    """Search packages across the wizard repos. Returns [(name, summary)] (unlimited)."""
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
    out = rank_matches(term, out)
    if sort_mode == "name":
        out = sorted(out)
    if out:
        rprint(f"[green]Found {len(out)} match(es) in {elapsed:.1f}s[/green]")
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


def _nevra_parts(nevra: str) -> tuple[str, str, str]:
    """name-version-release.arch -> (name, version_with_epoch, release)."""
    base = nevra.rsplit(".", 1)[0]
    parts = base.rsplit("-", 2)
    if len(parts) != 3:
        return (base, "", "")
    return (parts[0], parts[1], parts[2])


def _vkey(version: str) -> tuple:
    """Numeric-aware sort key for an RPM version string."""
    version = version.split(":", 1)[-1]  # drop epoch
    parts: list[tuple[int, int, str]] = []
    for chunk in re.split(r"[._\-+~]", version):
        m = re.match(r"(\d+)", chunk)
        if m:
            parts.append((1, int(m.group(1)), chunk))
        else:
            parts.append((0, 0, chunk))
    return tuple(parts)


def _version_le(a: str, b: str) -> bool:
    return _vkey(a) <= _vkey(b)


def verify_install(
    repos: list[dict],
    releasever: str,
    basearch: str,
    pkgs: list[str],
    installroot: str | None = None,
) -> tuple[bool, str | None]:
    """
    Co-installability check via `dnf install --assumeno` — a real transactional
    solve. Repo scope matches pack: system repos are NOT disabled (pack relies
    on them for base deps like libicu). --assumeno always exits non-zero
    ('Operation aborted.') even for valid transactions, so success/failure is
    detected by the presence of solver 'Problem' lines in the output.
    """
    cmd = [
        "dnf",
        "install",
        "--assumeno",
        "-y",
        "--setopt=install_weak_deps=False",
        "--setopt=cachedir=" + str(WIZARD_CACHE_DIR),
    ]
    if releasever:
        cmd.extend(["--releasever", releasever])
    if installroot:
        cmd.extend(["--installroot", installroot])
    cmd.extend(build_repo_opts(repos))
    cmd.extend(pkgs)
    res = _run(cmd, timeout=900)
    output = (res.stdout or "") + (res.stderr or "")
    if "Problem:" in output or "nothing provides" in output:
        lines = output.splitlines()
        idx = next(
            (i for i, l in enumerate(lines) if "Problem:" in l or "nothing provides" in l), 0
        )
        return False, "\n".join(lines[idx : idx + 8])
    return True, None


def resolve_download_set(
    repos: list[dict],
    releasever: str,
    basearch: str,
    pkgs: list[str],
    installroot: str | None = None,
) -> tuple[list[str], str | None]:
    """
    Run `dnf download --url --resolve` — the exact solver DnfPlugin.pack() uses —
    against the candidate pin list. Returns (package_strings, error_or_None).
    Package strings are derived from RPM filenames (name-version-release.arch).
    """
    if not pkgs:
        return [], None
    cmd = [
        "dnf",
        "download",
        "--url",
        "--resolve",
        "-y",
        "--setopt=strict=0",
        "--setopt=install_weak_deps=False",
        "--setopt=cachedir=" + str(WIZARD_CACHE_DIR),
        *build_repo_opts(repos),
        "--arch",
        f"{basearch},noarch",
    ]
    if releasever:
        cmd.extend(["--releasever", releasever])
    if installroot:
        cmd.extend(["--installroot", installroot])
    cmd.extend(pkgs)
    res = _run(cmd, timeout=900)
    if res.returncode != 0:
        detail = (res.stderr or res.stdout or "").strip().splitlines()
        problem = [
            l for l in detail if "Problem" in l or "nothing provides" in l or "conflicting" in l
        ]
        return [], "\n".join(problem[-6:] or detail[-6:])
    out: list[str] = []
    for line in res.stdout.splitlines():
        line = line.strip()
        if not any(line.startswith(s) for s in ("http://", "https://", "ftp://", "file://")):
            continue
        filename = line.split("/")[-1].split("?")[0]
        if filename.endswith(".rpm"):
            out.append(filename[:-4])
    return list(dict.fromkeys(out)), None


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
    add_repos=None,
) -> list[str]:
    """
    Interactive search/select loop. Returns a list of pinned nevra strings,
    or [] if the user cancels / nothing selected.

    `add_repos`: optional callable returning extra repo dicts; enables the
    "Add more upstream repos" action so repos and packages can be interleaved.
    """
    if not shutil.which("dnf"):
        rprint(
            "[yellow]dnf not available on this machine — skipping live package browsing.[/yellow]"
        )
        return []
    if not repos and not add_repos:
        rprint(
            "[yellow]No upstream repos selected — live browsing needs at least one repo (system repos are disabled in the wizard).[/yellow]"
        )
        return []

    sort_mode = "relevance"

    def _search_round() -> None:
        """One search/select cycle; appends to `selected`."""
        while True:
            if not repos:
                rprint("[yellow]Add an upstream repo first (search needs at least one).[/yellow]")
                break
            term = questionary.text(
                "Search packages (type name, press Enter; blank to finish):"
            ).ask()
            if not term or not term.strip():
                break
            matches = search_packages(repos, releasever, basearch, term, sort_mode)
            if not matches:
                rprint("[yellow]No matches. Try a shorter prefix.[/yellow]")
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

    def _pin_closure() -> list[str] | None:
        """Resolve + verify closure. Returns final pin list, or None to go back to the menu."""
        rprint("[cyan]Resolving transitive dependencies...[/cyan]")
        deps = resolve_deps(repos, releasever, basearch, selected, installroot=installroot)
        total = sum(size for _, size in deps)
        rprint(
            f"[cyan]Total resolved: {len(deps)} package(s), {_human_size(total)}"
            " [dim](preview — the pack solver picks compatible versions)[/dim][/cyan]"
        )
        for nevra, size in deps[:40]:
            rprint(f"[dim]  {nevra:<45} {_human_size(size)}[/dim]")
        if len(deps) > 40:
            rprint(f"[dim]  ... and {len(deps) - 40} more[/dim]")
        if (
            not deps
            or not questionary.confirm(
                "Pin the full dependency closure? (verified with dnf's transactional solver)",
                default=True,
            ).ask()
        ):
            return None

        sel_versions = {_nevra_parts(s)[1].split(":")[-1] for s in selected}
        pins = list(selected)
        for _attempt in range(6):
            download_set, error = resolve_download_set(
                repos, releasever, basearch, pins, installroot=installroot
            )
            if error is not None or not download_set:
                rprint("[red]Solver rejected the dependency closure:[/red]")
                if error:
                    for line in error.splitlines():
                        rprint(f"[yellow]  {line}[/yellow]")
                # Auto-recovery: parse "needed by X" and drop the offender.
                culprits = [c for c in re.findall(r"needed by (\S+)", error or "") if c in pins]
                if (
                    culprits
                    and questionary.confirm(
                        f"Drop {', '.join(culprits)} and retry? (its requirements will be met by alternatives)",
                        default=True,
                    ).ask()
                ):
                    pins = [p for p in pins if p not in culprits]
                    if not pins:
                        return None
                    continue
                break
            # Transactional co-installability check (matches pack's repo scope).
            ok, ierr = verify_install(
                repos, releasever, basearch, download_set, installroot=installroot
            )
            if ok:
                return download_set
            rprint("[red]Co-installability check failed:[/red]")
            for line in (ierr or "").splitlines():
                rprint(f"[yellow]  {line}[/yellow]")
            # Heal: when the download set contains two versions of the same
            # package (pinned 17.9 stack + 'best candidate' 17.11), constrain
            # the solver to the compatible single version and re-verify.
            pairs = re.findall(r"cannot install both (\S+) from \S+ and (\S+) from \S+", ierr or "")
            healed = False
            for a, b in pairs:
                an, av, _ = _nevra_parts(a)
                bn, bv, _ = _nevra_parts(b)
                av = av.split(":")[-1]
                bv = bv.split(":")[-1]
                if av in sel_versions and bv not in sel_versions:
                    keep = a
                elif bv in sel_versions and av not in sel_versions:
                    keep = b
                else:
                    keep = a if _version_le(av, bv) else b
                if keep not in pins:
                    pins.append(keep)
                    healed = True
                    rprint(f"[cyan]Healing version conflict — constraining solver to:[/] {keep}")
            if healed:
                continue
            break
        if questionary.confirm(
            "Keep top-level packages only and let pack resolve deps at pack time?",
            default=True,
        ).ask():
            return list(selected)
        return None

    if repos:
        warm_metadata(repos, releasever, basearch)
    rprint(
        "[cyan]Search packages, add repos, or finish — your selection is kept between steps.[/cyan]"
    )
    selected: list[str] = []
    _search_round()

    while True:
        if not selected:
            _search_round()
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
            return []
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
                warm_metadata(new, releasever, basearch)
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
        # Pin full dependency closure
        result = _pin_closure()
        if result is not None:
            return result
        # None -> back to the action menu (selection unchanged)
