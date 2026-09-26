"""Tests for the wizard field/search history store."""

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from syncit.wizard import history


@pytest.fixture(autouse=True)
def isolated_history(tmp_path: Path):
    hist_dir = tmp_path / "hist"
    hist_dir.mkdir()
    history.DEFAULT_PATH = hist_dir / "history.json"  # type: ignore[attr-defined]
    history.reset_cache()
    yield
    history.reset_cache()


def test_record_value_roundtrip_and_lru():
    history.record_value("bundle_name", "k8s")
    history.record_value("bundle_name", "monitoring")
    history.record_value("bundle_name", "k8s")  # dedup, moves to front
    assert history.recent_values("bundle_name") == ["k8s", "monitoring"]


def test_record_value_caps_at_max():
    for i in range(20):
        history.record_value("bundle_name", f"bundle-{i}")
    vals = history.recent_values("bundle_name")
    assert len(vals) == history.MAX_HISTORY
    assert vals[0] == "bundle-19"  # most recent first
    assert "bundle-0" not in vals


def test_record_value_ignores_blank():
    history.record_value("bundle_name", "   ")
    assert history.recent_values("bundle_name") == []


def test_persists_across_cache_reset():
    history.record_value("bundle_name", "edge")
    history.reset_cache()
    assert history.recent_values("bundle_name") == ["edge"]


def test_corrupt_file_treated_as_empty(tmp_path: Path):
    history.record_value("bundle_name", "first")
    history.DEFAULT_PATH.write_text("{not json", encoding="utf-8")  # type: ignore[attr-defined]
    history.reset_cache()
    assert history.recent_values("bundle_name") == []


def test_search_history_per_browser():
    history.record_search("apt", "kubeadm")
    history.record_search("dnf", "podman")
    assert history.recent_searches("apt") == ["kubeadm"]
    assert history.recent_searches("dnf") == ["podman"]
    assert history.recent_searches("pypi") == []


def test_repo_history_keyed_by_plugin_codename():
    history.record_repos("dnf", "9", ["baseos", "appstream"])
    history.record_repos("dnf", "", ["rocky-fallback"])
    assert history.recent_repos("dnf", "9") == ["appstream", "baseos"]
    assert history.recent_repos("dnf", "") == ["rocky-fallback"]


def test_prompt_with_history_empty_goes_to_text():
    with patch.object(
        history.questionary, "text", return_value=type("Q", (), {"ask": lambda self: "fresh"})()
    ) as mock_text:
        result = history.prompt_with_history("bundle_name", "Bundle name:")
    assert result == "fresh"
    mock_text.assert_called_once()
    assert history.recent_values("bundle_name") == ["fresh"]


def test_prompt_with_history_select_recent():
    history.record_value("bundle_name", "edge")
    picks = iter(["edge"])

    def fake_select(message, choices, **kwargs):
        return type("Q", (), {"ask": lambda self: next(picks)})()

    with patch.object(history.questionary, "select", fake_select):
        result = history.prompt_with_history("bundle_name", "Bundle name:")
    assert result == "edge"


def test_prompt_with_history_new_value_option():
    history.record_value("bundle_name", "edge")
    seq = iter([history.NEW_VALUE])

    def fake_select(message, choices, **kwargs):
        values = [getattr(c, "value", c) for c in choices]
        assert history.NEW_VALUE in values
        return type("Q", (), {"ask": lambda self: next(seq)})()

    def fake_text(message, default=""):
        assert default == "edge"
        return type("Q", (), {"ask": lambda self: "brand-new"})()

    with (
        patch.object(history.questionary, "select", fake_select),
        patch.object(history.questionary, "text", fake_text),
    ):
        result = history.prompt_with_history("bundle_name", "Bundle name:", default="edge")
    assert result == "brand-new"
    assert history.recent_values("bundle_name") == ["brand-new", "edge"]


def test_prompt_with_history_default_surfaces_first():
    history.record_value("bundle_name", "old")

    def fake_select(message, choices, **kwargs):
        values = [getattr(c, "value", c) for c in choices]
        assert values[0] == "cur"  # default surfaces at the top
        return type("Q", (), {"ask": lambda self: "cur"})()

    with patch.object(history.questionary, "select", fake_select):
        result = history.prompt_with_history("bundle_name", "Bundle name:", default="cur")
    assert result == "cur"


def test_prompt_search_records_new_term():
    def fake_text(message, **kwargs):
        return type("Q", (), {"ask": lambda self: "nginx"})()

    with patch.object(history.questionary, "text", fake_text):
        term = history.prompt_search("apt", "Search:")
    assert term == "nginx"
    assert history.recent_searches("apt") == ["nginx"]


def test_prompt_search_passes_arrow_history():
    history.record_search("dnf", "podman")
    seen = {}

    def fake_text(message, history=None, **kwargs):
        seen["history"] = history
        return type("Q", (), {"ask": lambda self: "podman"})()

    with patch.object(history.questionary, "text", fake_text):
        term = history.prompt_search("dnf", "Search:")
    assert term == "podman"
    assert seen["history"] is not None
    assert "podman" in list(seen["history"].get_strings())


def test_prompt_search_cancel_returns_none():
    with patch.object(
        history.questionary, "text", lambda *a, **k: type("Q", (), {"ask": lambda self: None})()
    ):
        assert history.prompt_search("apt", "Search:") is None


def test_prompt_text_history_roundtrip():
    history.record_value("bundle_name", "k8s")
    seen = {}

    def fake_text(message, default="", history=None, **kwargs):
        seen["message"] = message
        seen["default"] = default
        seen["history"] = history
        return type("Q", (), {"ask": lambda self: "edge"})()

    with patch.object(history.questionary, "text", fake_text):
        result = history.prompt_text_history("bundle_name", "Bundle name:", default="k8s")
    assert result == "edge"
    assert seen["message"] == "Bundle name:"
    assert seen["default"] == "k8s"
    assert "k8s" in list(seen["history"].get_strings())
    assert history.recent_values("bundle_name") == ["edge", "k8s"]


def test_prompt_text_history_empty_history_no_crash():
    with patch.object(
        history.questionary,
        "text",
        lambda *a, **k: type("Q", (), {"ask": lambda self: "fresh"})(),
    ):
        assert history.prompt_text_history("bundle_name", "Bundle name:") == "fresh"


def test_prompt_text_history_blank_not_recorded():
    with patch.object(
        history.questionary, "text", lambda *a, **k: type("Q", (), {"ask": lambda self: ""})()
    ):
        result = history.prompt_text_history("bundle_name", "Bundle name:")
    assert result == ""
    assert history.recent_values("bundle_name") == []


def test_on_disk_format():
    history.record_value("bundle_name", "k8s")
    data = json.loads(history.DEFAULT_PATH.read_text(encoding="utf-8"))  # type: ignore[attr-defined]
    assert data["fields"]["bundle_name"] == ["k8s"]


def test_clear_history_single_field():
    history.record_value("bundle_name", "k8s")
    history.record_search("apt", "nginx")
    assert history.clear_history("bundle_name") is True
    assert history.recent_values("bundle_name") == []
    assert history.recent_searches("apt") == ["nginx"]


def test_clear_history_all():
    history.record_value("bundle_name", "k8s")
    history.record_search("apt", "nginx")
    assert history.clear_history() is True
    assert history.recent_values("bundle_name") == []
    assert history.recent_searches("apt") == []


def test_missing_file_is_empty():
    assert history.recent_values("bundle_name") == []
    assert history.clear_history() is False
