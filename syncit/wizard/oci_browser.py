"""Docker Hub image browser for the `syncit create` wizard.

Searches Docker Hub's public API (stdlib urllib), lists tags for a chosen
repository (sha256/metadata tags filtered out), and returns fully qualified
image references like "nginx:1.29" or "bitnami/redis:7.4". Other registries
can be entered manually (the oci_image plugin supports arbitrary refs).
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

import questionary
from rich import print as rprint


def _fetch_json(url: str, timeout: int = 30) -> dict[str, Any] | None:
    req = urllib.request.Request(url, headers={"User-Agent": "syncit-wizard/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except (urllib.error.HTTPError, urllib.error.URLError, json.JSONDecodeError, TimeoutError):
        return None


def search_dockerhub(
    term: str, page: int = 1, page_size: int = 100
) -> tuple[list[tuple[str, str]], int]:
    """
    Search Docker Hub. Returns ([(repo_name, 'desc | ★ stars | ⬇ pulls')], total_count).
    """
    term = term.strip()
    data = _fetch_json(
        "https://hub.docker.com/v2/search/repositories/"
        f"?query={urllib.parse.quote(term)}&page={page}&page_size={page_size}"
    )
    if not data:
        rprint("[yellow]Docker Hub search failed (network or API error).[/yellow]")
        return [], 0
    out: list[tuple[str, str]] = []
    for r in data.get("results", []):
        repo = r.get("repo_name") or r.get("name") or ""
        if not repo:
            continue
        stars = r.get("star_count") or 0
        pulls = r.get("pull_count") or 0
        desc = (r.get("short_description") or "")[:55]
        out.append((repo, f"{desc} [★ {stars} | ⬇ {_fmt_count(pulls)}]"))
    return out, int(data.get("count") or 0)


def _fmt_count(n: int) -> str:
    value = float(n)
    for unit in ("", "K", "M", "B"):
        if value < 1000:
            return f"{value:.0f}{unit}"
        value /= 1000
    return f"{value:.0f}T"


def _add_from_registry(selected: list[str]) -> None:
    """Structured flow for non-Docker-Hub registries (quay/k8s/ghcr/gcr/custom)."""
    from syncit.wizard.registries import format_ref, pick_registry

    adapter = pick_registry()
    if adapter is None:
        return

    if adapter.supports_search:
        term = questionary.text(
            f"Search {adapter.name} (blank to enter a repo path instead):"
        ).ask()
        if term and term.strip():
            results, total = adapter.search(term)
            if not results:
                rprint("[yellow]No matches on this registry.[/yellow]")
            choices = [questionary.Choice(title=f"{n:<38}  {s[:70]}", value=n) for n, s in results]
            picked = (
                questionary.checkbox(
                    f"Select images on {adapter.name} (space to toggle):",
                    choices=choices,
                    use_search_filter=True,
                    use_jk_keys=False,
                ).ask()
                or []
            )
            for repo in picked:
                tags = adapter.tags(repo)
                if not tags:
                    continue
                tag = questionary.select(
                    f"Tag for {repo}:",
                    choices=[questionary.Choice(title=t, value=t) for t in tags],
                ).ask()
                if tag:
                    ref = format_ref(adapter.name, repo, tag)
                    if ref not in selected:
                        selected.append(ref)
                        rprint(f"[green]Added:[/] {ref}")
            return
        term = ""

    # No search: known paths or manual repo path
    quick = adapter.quick_repos()
    repo = None
    if quick:
        choices = [questionary.Choice(title=q, value=q) for q in quick]
        choices.append(
            questionary.Choice(title="Other (type the repository path)", value="__other__")
        )
        picked = questionary.select(f"Repository on {adapter.name}:", choices=choices).ask()
        if picked is None:
            return
        repo = (
            questionary.text(f"Repository path on {adapter.name} (e.g. org/app):").ask()
            if picked == "__other__"
            else picked
        )
    else:
        repo = questionary.text(f"Repository path on {adapter.name} (e.g. org/app):").ask()
    if not repo or not repo.strip():
        return
    repo = repo.strip()
    tags = adapter.tags(repo)
    if tags:
        tag = questionary.select(
            f"Tag for {adapter.name}/{repo}:",
            choices=[questionary.Choice(title=t, value=t) for t in tags],
        ).ask()
        if tag:
            ref = format_ref(adapter.name, repo, tag)
            if ref not in selected:
                selected.append(ref)
                rprint(f"[green]Added:[/] {ref}")
            return
    # Tag listing failed (private repo?) — manual entry
    ref = questionary.text(
        f"Full image reference on {adapter.name} (e.g. {adapter.name}/org/app:tag):"
    ).ask()
    if ref and ref.strip() and ref.strip() not in selected:
        selected.append(ref.strip())
        rprint(f"[green]Added:[/] {ref.strip()}")


def list_tags(repo: str, limit: int = 30) -> list[str]:
    """List usable tags for a repo, newest/latest-flavored first."""
    if "/" not in repo:
        repo = f"library/{repo}"
    data = _fetch_json(
        f"https://hub.docker.com/v2/repositories/{repo}/tags?page_size={min(limit * 3, 100)}"
    )
    if not data:
        rprint(f"[yellow]Could not fetch tags for {repo}.[/yellow]")
        return []
    tags: list[str] = []
    for t in data.get("results", []):
        name = t.get("name") or ""
        # skip digests and digest-derivative metadata tags
        if not name or name.startswith("sha256-") or name.endswith((".sig", ".metadata")):
            continue
        tags.append(name)
        if len(tags) >= limit:
            break
    return tags


def _looks_like_ref(term: str) -> bool:
    """True when the term is a full image reference rather than a search term
    (has a tag — 'x/y:tag' — or a registry host — 'registry.k8s.io/...')."""
    term = term.strip()
    if not term:
        return False
    first = term.split("/")[0]
    if "." in first or first == "localhost":
        return True
    if ":" in term.rsplit("/", 1)[-1]:
        return True
    return False


def _handle_full_ref(term: str, selected: list[str]) -> None:
    """User pasted a complete image ref — resolve tags on the right registry."""
    from syncit.wizard.registries import (
        OciV2Registry,
        REGISTRY_ADAPTERS,
        format_ref,
        parse_image_ref,
    )

    registry, repo, tag = parse_image_ref(term)
    if tag:
        ref = format_ref(registry, repo, tag)
        if ref not in selected:
            selected.append(ref)
        rprint(f"[green]Added:[/] {ref}")
        return
    adapter = REGISTRY_ADAPTERS.get(registry)
    if adapter is None:
        adapter = OciV2Registry(registry)
    tags = adapter.tags(repo)
    if not tags:
        ref = format_ref(registry, repo, None)
        entered = questionary.text(f"Tag for {ref} (blank = no tag):").ask()
        if entered is None:
            return
        ref = format_ref(registry, repo, entered.strip() or None)
        if ref not in selected:
            selected.append(ref)
            rprint(f"[green]Added:[/] {ref}")
        return
    chosen = questionary.select(
        f"Tag for {registry}/{repo}:",
        choices=[questionary.Choice(title=t, value=t) for t in tags],
    ).ask()
    if chosen:
        ref = format_ref(registry, repo, chosen)
        if ref not in selected:
            selected.append(ref)
            rprint(f"[green]Added:[/] {ref}")


def _apply_sort(items: list[tuple[str, str]], mode: str, term: str) -> list[tuple[str, str]]:
    from syncit.wizard.ranking import rank_matches

    if mode == "name":
        return sorted(items)
    if mode in ("stars", "pulls"):

        def metric(item: tuple[str, str]) -> int:
            summary = item[1]
            marker = "★" if mode == "stars" else "⬇"
            if marker not in summary:
                return 0
            tail = summary.rsplit(marker, 1)[-1].strip(" |]")
            try:
                num = float(tail.rstrip("KMBT") or 0)
                unit = tail[len(tail.rstrip("KMBT")) :]
                mult = {"": 1, "K": 1e3, "M": 1e6, "B": 1e9, "T": 1e12}.get(unit, 1)
                return int(num * mult)
            except ValueError:
                return 0

        return sorted(items, key=metric, reverse=True)
    return rank_matches(term, items)


_SORT_LABELS = {
    "relevance": "relevance (best match first)",
    "stars": "GitHub-style stars",
    "pulls": "Docker Hub pulls",
    "name": "name (A-Z)",
}


def browse_oci_images(
    registries: list[str] | None = None,
    prompt_label: str = "Docker Hub",
) -> list[str] | None:
    """
    Interactive Docker Hub search/select loop with pagination ("fetch more")
    and sorting (relevance / stars / pulls / name). Returns image references
    ("nginx:1.29", "bitnami/redis:7.4") suitable for the oci_image plugin,
    or None when the user explicitly cancels.
    """
    if not sys.stdout.isatty():
        return []
    selected: list[str] = []
    sort_mode = "relevance"

    def _search_round() -> bool:
        from syncit.wizard.history import prompt_search

        while True:
            term = prompt_search(
                "docker",
                f"Search {prompt_label} or paste a full image ref (Enter to search):",
                finish="Finish",
            )
            if term is None:
                return True
            if not term or not term.strip():
                return False
            if _looks_like_ref(term):
                _handle_full_ref(term, selected)
                continue
            page = 1
            fetched: set[str] = set()
            while True:
                page_results, total = search_dockerhub(term, page=page)
                if not page_results:
                    if not fetched:
                        rprint("[yellow]No matches. Try a different name.[/yellow]")
                    break
                new = [(n, s) for n, s in page_results if n not in fetched]
                fetched.update(n for n, _ in page_results)
                shown = _apply_sort(new, sort_mode, term)
                choices = [
                    questionary.Choice(title=f"{n:<38}  {s[:70]}", value=n) for n, s in shown
                ]
                picked = (
                    questionary.checkbox(
                        f"Select images (page {page}, {len(fetched)}/{total} shown; space to toggle):",
                        choices=choices,
                        use_search_filter=True,
                        use_jk_keys=False,
                    ).ask()
                    or []
                )
                for repo in picked:
                    tags = list_tags(repo)
                    if not tags:
                        continue
                    tag = questionary.select(
                        f"Tag for {repo}:",
                        choices=[questionary.Choice(title=t, value=t) for t in tags],
                    ).ask()
                    if not tag:
                        continue
                    ref = f"{repo}:{tag}"
                    if ref not in selected:
                        selected.append(ref)
                        rprint(f"[green]Added:[/] {ref}")
                if total > len(fetched):
                    if not questionary.confirm(
                        f"Fetch more results? ({total - len(fetched)} remaining)",
                        default=False,
                    ).ask():
                        break
                    page += 1
                else:
                    break

    if _search_round():
        return None
    while True:
        if not selected:
            if _search_round():
                return None
            if not selected:
                return []
        choices: list[str] = []
        if registries:
            choices.append("Add image from another registry (manual)")
        choices += [
            "Search again",
            f"Change sorting (current: {_SORT_LABELS.get(sort_mode, sort_mode)})",
            "Review / remove images",
            "Use as-is",
            "Cancel browsing",
        ]
        action = questionary.select("Image selection:", choices=choices).ask()
        if action is None or action == "Cancel browsing":
            return None
        if action == "Use as-is":
            return selected
        if action.startswith("Change sorting"):
            order = list(_SORT_LABELS)
            sort_mode = order[(order.index(sort_mode) + 1) % len(order)]
            rprint(f"[cyan]Results will be sorted by: {_SORT_LABELS[sort_mode]}[/cyan]")
            continue
        if action == "Search again":
            _search_round()
            continue
        if action == "Add image from another registry (manual)":
            _add_from_registry(selected)
            continue
        # Review
        keep = (
            questionary.checkbox(
                "Keep these images (uncheck to remove):",
                choices=[questionary.Choice(title=s, value=s, checked=True) for s in selected],
            ).ask()
            or []
        )
        selected = [s for s in selected if s in keep]
