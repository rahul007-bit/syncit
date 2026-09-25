"""Registry adapters for the OCI image wizard.

Search APIs are NOT standard across registries:
  - docker.io  : Hub search API (full search + tags)
  - quay.io    : quay find API (full search + tags)
  - OCI v2     : registry.k8s.io, gcr.io, ghcr.io, Harbor — the standard
                 /v2/<name>/tags/list endpoint works anonymously for public
                 repos (ghcr needs the anonymous-token dance), but there is
                 NO search — you must know the repository path.
  - ACR/private: authentication required — manual reference entry only.

Every adapter returns fully-qualified references (registry/repo:tag) so the
oci_image plugin pulls from exactly the registry the user picked.
"""

from __future__ import annotations

import json
import urllib.parse
import urllib.request
from typing import Any

import questionary
from rich import print as rprint

DEFAULT_REGISTRY = "docker.io"


def _get_json(url: str, headers: dict[str, str] | None = None, timeout: int = 30) -> Any:
    req = urllib.request.Request(
        url, headers={"User-Agent": "syncit-wizard/1.0", **(headers or {})}
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except Exception:
        return None


# ── Reference parsing ────────────────────────────────────────────────────


def parse_image_ref(ref: str) -> tuple[str, str, str | None]:
    """
    "nginx:1.29"            -> ("docker.io", "library/nginx", "1.29")
    "bitnami/redis:7.4"     -> ("docker.io", "bitnami/redis", "7.4")
    "quay.io/org/app:tag"   -> ("quay.io", "org/app", "tag")
    "registry.k8s.io/kube-apiserver" -> ("registry.k8s.io", "kube-apiserver", None)
    """
    ref = ref.strip()
    registry = DEFAULT_REGISTRY
    if "/" in ref and ("." in ref.split("/")[0] or ref.split("/")[0] == "localhost"):
        registry, ref = ref.split("/", 1)
    tag = None
    if ":" in ref.rsplit("/", 1)[-1]:
        ref, _, tag = ref.rpartition(":")
    if registry == DEFAULT_REGISTRY and "/" not in ref:
        ref = f"library/{ref}"
    return registry, ref, tag


def format_ref(registry: str, repo: str, tag: str | None = None) -> str:
    ref = f"{registry}/{repo}" if registry != DEFAULT_REGISTRY else repo
    return f"{ref}:{tag}" if tag else ref


# ── Adapters ─────────────────────────────────────────────────────────────


class RegistryAdapter:
    name: str = ""
    supports_search: bool = False

    def search(self, term: str, page: int = 1) -> tuple[list[tuple[str, str]], int]:
        return [], 0

    def tags(self, repo: str) -> list[str]:
        return []

    def quick_repos(self) -> list[str]:
        """Known repository paths offered as shortcuts when search is unavailable."""
        return []


class DockerHubRegistry(RegistryAdapter):
    name = "docker.io"
    supports_search = True

    def search(self, term: str, page: int = 1) -> tuple[list[tuple[str, str]], int]:
        from syncit.wizard.oci_browser import search_dockerhub

        return search_dockerhub(term, page=page)

    def tags(self, repo: str) -> list[str]:
        from syncit.wizard.oci_browser import list_tags

        return list_tags(repo)


class QuayRegistry(RegistryAdapter):
    name = "quay.io"
    supports_search = True

    def search(self, term: str, page: int = 1) -> tuple[list[tuple[str, str]], int]:
        data = _get_json(
            f"https://quay.io/api/v1/find/all?query={urllib.parse.quote(term)}&page={page}"
        )
        if not data:
            return [], 0
        out: list[tuple[str, str]] = []
        for r in data.get("results", []):
            name = r.get("name") or ""
            ns = (r.get("namespace") or {}).get("name", "")
            repo = f"{ns}/{name}" if ns and name else name
            desc = (r.get("description") or "")[:55]
            pulls = r.get("pull_count") or 0
            out.append((repo, f"{desc} [⬇ {pulls}]"))
        return out, int(data.get("total_count") or len(out))

    def tags(self, repo: str) -> list[str]:
        if "/" not in repo:
            return []
        data = _get_json(
            f"https://quay.io/api/v1/repository/{repo}/tag/?limit=50&onlyActiveTags=true"
        )
        if not data:
            return []
        tags = [t.get("name", "") for t in data.get("tags", []) if t.get("name")]
        return [t for t in tags if not t.startswith("sha256-")][:30]


class OciV2Registry(RegistryAdapter):
    """Standard OCI Distribution API — tags only, no search."""

    name: str = ""
    token_service: str | None = None

    def __init__(self, host: str, token_service: str | None = None, quick: list[str] | None = None):
        self.name = host
        self.token_service = token_service
        self._quick = quick or []

    def tags(self, repo: str) -> list[str]:
        url = f"https://{self.name}/v2/{repo}/tags/list"
        headers: dict[str, str] = {}
        if self.token_service:
            token = self._anon_token(repo)
            if not token:
                return []
            headers["Authorization"] = f"Bearer {token}"
        req = urllib.request.Request(url, headers={"User-Agent": "syncit-wizard/1.0", **headers})
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read().decode())
        except Exception:
            rprint(
                f"[yellow]Tag listing failed for {self.name}/{repo} (repo may be private).[/yellow]"
            )
            return []
        tags = data.get("tags") or []
        from syncit.wizard.rpm_browser import _vkey

        return sorted([t for t in tags if t], key=_vkey, reverse=True)[:40]

    def _anon_token(self, repo: str) -> str | None:
        if not self.token_service:
            return None
        try:
            url = (
                f"https://{self.name}/token?service={urllib.parse.quote(self.token_service)}"
                f"&scope=repository:{urllib.parse.quote(repo)}:pull"
            )
            data = _get_json(url)
            return (data or {}).get("token")
        except Exception:
            return None

    def quick_repos(self) -> list[str]:
        return self._quick


# Well-known registries
K8S_QUICK = [
    "kube-apiserver",
    "kube-controller-manager",
    "kube-scheduler",
    "kube-proxy",
    "etcd",
    "coredns",
    "pause",
    "conformance",
]

REGISTRY_ADAPTERS: dict[str, RegistryAdapter] = {
    a.name: a
    for a in [
        DockerHubRegistry(),
        QuayRegistry(),
        OciV2Registry("registry.k8s.io", quick=K8S_QUICK),
        OciV2Registry("ghcr.io", token_service="ghcr.io"),
        OciV2Registry("gcr.io"),
        OciV2Registry("registry-1.docker.io", token_service="docker.io"),
    ]
}


def pick_registry() -> RegistryAdapter | None:
    """Interactive registry chooser; returns the selected adapter or None."""
    choices = [
        questionary.Choice(title="docker.io — Docker Hub (search available)", value="docker.io"),
        questionary.Choice(title="quay.io — Red Hat Quay (search available)", value="quay.io"),
        questionary.Choice(
            title="registry.k8s.io — Kubernetes images (no search, known paths)",
            value="registry.k8s.io",
        ),
        questionary.Choice(
            title="ghcr.io — GitHub Container Registry (no search)", value="ghcr.io"
        ),
        questionary.Choice(title="gcr.io — Google Container Registry (no search)", value="gcr.io"),
        questionary.Choice(
            title="Custom / private (ACR, Harbor…) — manual reference", value="__custom__"
        ),
    ]
    picked = questionary.select("Registry:", choices=choices).ask()
    if picked is None:
        return None
    if picked == "__custom__":
        host = questionary.text("Registry host (e.g. myregistry.azurecr.io):").ask()
        if not host or not host.strip():
            return None
        return OciV2Registry(host.strip())
    return REGISTRY_ADAPTERS.get(picked)
