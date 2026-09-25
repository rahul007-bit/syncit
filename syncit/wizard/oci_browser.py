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
) -> list[str]:
    """
    Interactive Docker Hub search/select loop with pagination ("fetch more")
    and sorting (relevance / stars / pulls / name). Returns image references
    ("nginx:1.29", "bitnami/redis:7.4") suitable for the oci_image plugin.
    """
    if not sys.stdout.isatty():
        return []
    selected: list[str] = []
    sort_mode = "relevance"

    def _search_round() -> None:
        while True:
            term = questionary.text(f"Search {prompt_label} (image name, blank to finish):").ask()
            if not term or not term.strip():
                break
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

    _search_round()
    while True:
        if not selected:
            _search_round()
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
            return []
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
            ref = questionary.text("Full image reference (e.g. quay.io/x/y:tag):").ask()
            if ref and ref.strip() and ref.strip() not in selected:
                selected.append(ref.strip())
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
