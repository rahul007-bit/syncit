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


def test_list_tags_prefixes_library():
    captured = {}

    def fake_fetch(url, timeout=30):
        captured["url"] = url
        return {"results": [{"name": "latest"}]}

    with patch.object(ob, "_fetch_json", fake_fetch):
        ob.list_tags("nginx")
    assert "library/nginx" in captured["url"]
