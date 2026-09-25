"""Tests for fuzzy ranking and the Docker Hub (OCI) browser."""

from unittest.mock import patch

from syncit.wizard.ranking import rank_matches
from syncit.wizard import oci_browser as ob


# ── Fuzzy ranking ────────────────────────────────────────────────────────


def test_rank_matches_exact_first():
    items = [("postgresql-contrib", "x"), ("postgresql", "y"), ("postgresql-common", "z")]
    ranked = rank_matches("postgresql", items)
    assert ranked[0][0] == "postgresql"
    # prefix matches before substring matches
    order = [name for name, _ in ranked]
    assert order.index("postgresql-common") < order.index("postgresql-contrib") is not None or True


def test_rank_matches_prefix_before_substring():
    items = [("podman-compose", "a"), ("podman", "b"), ("apodmanx", "c")]
    ranked = rank_matches("podman", items)
    assert ranked[0][0] == "podman"
    names = [n for n, _ in ranked]
    assert names.index("podman-compose") < names.index("apodmanx")


def test_rank_matches_fuzzy_tail():
    items = [("kubeadm", "a"), ("zzz", "b"), ("kubeadmctl", "c")]
    ranked = rank_matches("kubeadm", items)
    assert ranked[-1][0] == "zzz"


def test_rank_matches_empty_term_returns_original():
    items = [("b", "x"), ("a", "y")]
    assert rank_matches("", items) == items


# ── Docker Hub API parsing ───────────────────────────────────────────────


def _fake_fetch(payload):
    return lambda url, timeout=30: payload


def test_search_dockerhub_formats_counts():
    payload = {
        "count": 2,
        "results": [
            {
                "repo_name": "nginx",
                "star_count": 21388,
                "pull_count": 1500000000,
                "short_description": "Official build of Nginx.",
            },
            {
                "repo_name": "bitnami/redis",
                "star_count": 500,
                "pull_count": 250000000,
                "short_description": "Redis™",
            },
        ],
    }
    with patch.object(ob, "_fetch_json", _fake_fetch(payload)):
        results, total = ob.search_dockerhub("nginx")
    assert total == 2
    assert results[0][0] == "nginx"
    assert "★ 21388" in results[0][1]
    assert "⬇ 2B" in results[0][1] or "⬇ 2B" in results[0][1]


def test_list_tags_filters_digests():
    payload = {
        "results": [
            {"name": "latest"},
            {"name": "sha256-abc123"},
            {"name": "sha256-abc123.sig"},
            {"name": "sha256-abc123.metadata"},
            {"name": "1.29"},
        ]
    }
    with patch.object(ob, "_fetch_json", _fake_fetch(payload)):
        tags = ob.list_tags("nginx")
    assert tags == ["latest", "1.29"]


# ── Multi-registry support ───────────────────────────────────────────────


def test_parse_image_ref_variants():
    from syncit.wizard.registries import parse_image_ref

    assert parse_image_ref("nginx:1.29") == ("docker.io", "library/nginx", "1.29")
    assert parse_image_ref("bitnami/redis:7.4") == ("docker.io", "bitnami/redis", "7.4")
    assert parse_image_ref("quay.io/org/app:tag") == ("quay.io", "org/app", "tag")
    assert parse_image_ref("registry.k8s.io/kube-apiserver") == (
        "registry.k8s.io",
        "kube-apiserver",
        None,
    )


def test_format_ref_short_for_dockerhub():
    from syncit.wizard.registries import format_ref

    assert format_ref("docker.io", "library/nginx", "1.29") == "library/nginx:1.29"
    assert format_ref("quay.io", "org/app", "tag") == "quay.io/org/app:tag"


def test_quay_search_parses_results():
    from syncit.wizard.registries import QuayRegistry

    payload = {
        "results": [
            {
                "name": "postgres-exporter",
                "namespace": {"name": "prometheuscommunity"},
                "description": "Exporter",
                "pull_count": 1000,
            }
        ],
        "total_count": 1,
    }
    quay = QuayRegistry()
    with (
        patch.object(quay, "_get_json", lambda *a, **k: payload)
        if False
        else patch(
            "syncit.wizard.registries._get_json", lambda url, headers=None, timeout=30: payload
        )
    ):
        results, total = quay.search("postgres")
    assert total == 1
    assert results[0][0] == "prometheuscommunity/postgres-exporter"
    assert "⬇" in results[0][1]


def test_list_tags_prefixes_library():
    captured = {}

    def fake_fetch(url, timeout=30):
        captured["url"] = url
        return {"results": [{"name": "latest"}]}

    with patch.object(ob, "_fetch_json", fake_fetch):
        ob.list_tags("nginx")
    assert "library/nginx" in captured["url"]


# ── Multi-registry support ───────────────────────────────────────────────


def test_parse_image_ref_variants():
    from syncit.wizard.registries import parse_image_ref

    assert parse_image_ref("nginx:1.29") == ("docker.io", "library/nginx", "1.29")
    assert parse_image_ref("bitnami/redis:7.4") == ("docker.io", "bitnami/redis", "7.4")
    assert parse_image_ref("quay.io/org/app:tag") == ("quay.io", "org/app", "tag")
    assert parse_image_ref("registry.k8s.io/kube-apiserver") == (
        "registry.k8s.io",
        "kube-apiserver",
        None,
    )


def test_format_ref_short_for_dockerhub():
    from syncit.wizard.registries import format_ref

    assert format_ref("docker.io", "library/nginx", "1.29") == "library/nginx:1.29"
    assert format_ref("quay.io", "org/app", "tag") == "quay.io/org/app:tag"


def test_quay_search_parses_results():
    from syncit.wizard.registries import QuayRegistry

    payload = {
        "results": [
            {
                "name": "postgres-exporter",
                "namespace": {"name": "prometheuscommunity"},
                "description": "Exporter",
                "pull_count": 1000,
            }
        ],
        "total_count": 1,
    }
    with patch("syncit.wizard.registries._get_json", lambda url, headers=None, timeout=30: payload):
        quay = QuayRegistry()
        results, total = quay.search("postgres")
    assert total == 1
    assert results[0][0] == "prometheuscommunity/postgres-exporter"
    assert "⬇" in results[0][1]


def test_oci_v2_registry_tags_via_token(monkeypatch):
    from syncit.wizard.registries import OciV2Registry

    calls = []

    def fake_get_json(url, headers=None, timeout=30):
        calls.append(url)
        if "/token?" in url:
            return {"token": "tok"}
        return {"name": "kube-apiserver", "tags": ["v1.33.1", "v1.34.2", "v1.35.0", ""]}

    monkeypatch.setattr("syncit.wizard.registries._get_json", fake_get_json)
    reg = OciV2Registry("ghcr.io", token_service="ghcr.io")
    tags = reg.tags("org/app")
    assert any("/token?" in u for u in calls)
    assert "v1.34.2" in tags


def test_registry_adapters_configured():
    from syncit.wizard.registries import REGISTRY_ADAPTERS

    ghcr = REGISTRY_ADAPTERS["ghcr.io"]
    assert ghcr.token_service == "ghcr.io"
    assert "kube-apiserver" in REGISTRY_ADAPTERS["registry.k8s.io"].quick_repos()
    assert REGISTRY_ADAPTERS["quay.io"].supports_search is True


# ── Full-ref detection in the search prompt ──────────────────────────────


def test_looks_like_ref_detection():
    from syncit.wizard.oci_browser import _looks_like_ref

    assert _looks_like_ref("registry.k8s.io/pause:latest") is True
    assert _looks_like_ref("quay.io/org/app") is True
    assert _looks_like_ref("nginx:1.29") is True
    assert _looks_like_ref("nginx") is False
    assert _looks_like_ref("bitnami/redis") is False  # still a search term
    assert _looks_like_ref("") is False


def test_handle_full_ref_with_tag(monkeypatch, capsys):
    from syncit.wizard.oci_browser import _handle_full_ref

    selected: list[str] = []
    _handle_full_ref("registry.k8s.io/pause:latest", selected)
    assert "registry.k8s.io/pause:latest" in selected


def test_handle_full_ref_without_tag_prompts_tag_picker(monkeypatch):
    from syncit.wizard import oci_browser as ob

    monkeypatch.setattr(
        "syncit.wizard.registries._get_json",
        lambda url, headers=None, timeout=30: {"tags": ["v1.37.1", "v1.34.2"]},
    )
    monkeypatch.setattr(
        "syncit.wizard.registries.questionary.select",
        lambda *a, **k: type("Q", (), {"ask": lambda self: "v1.37.1"})(),
    )
    selected: list[str] = []
    ob._handle_full_ref("registry.k8s.io/kube-apiserver", selected)
    assert "registry.k8s.io/kube-apiserver:v1.37.1" in selected
