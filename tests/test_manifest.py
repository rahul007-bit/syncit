"""Tests for manifest loading and schema validation."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from syncit.manifest.loader import load_manifest
from syncit.manifest.schema import TaskSpec


class TestLoadManifest:
    def test_load_valid_manifest(self, bundle_yaml: Path) -> None:
        """A valid bundle.yaml should parse without errors."""
        manifest = load_manifest(bundle_yaml)
        assert manifest.metadata.name == "test-env"
        assert manifest.metadata.version == "1.0.0"
        assert manifest.metadata.author == "tester"

    def test_tasks_extracted_correctly(self, bundle_yaml: Path) -> None:
        """Tasks should be extracted with correct plugin names and configs."""
        manifest = load_manifest(bundle_yaml)
        tasks = manifest.get_tasks()
        assert len(tasks) == 3

        apt_task = tasks[0]
        assert apt_task.plugin == "apt"
        assert "packages" in apt_task.config

        pip_task = tasks[1]
        assert pip_task.plugin == "pip"
        assert pip_task.config.get("python_version") == "3.11"

        oci_task = tasks[2]
        assert oci_task.plugin == "oci_image"
        assert len(oci_task.config["images"]) == 2

    def test_get_targets(self, bundle_yaml: Path) -> None:
        manifest = load_manifest(bundle_yaml)
        targets = manifest.get_targets()
        assert targets.distro == "ubuntu"
        assert targets.codename == "noble"
        assert targets.arch == "amd64"

    def test_file_not_found_raises(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError, match="not found"):
            load_manifest(tmp_path / "nonexistent.yaml")

    def test_bad_yaml_raises(self, tmp_path: Path) -> None:
        bad_yaml = tmp_path / "bad.yaml"
        bad_yaml.write_text("{invalid: yaml: content: [")
        with pytest.raises(ValueError, match="Failed to parse YAML"):
            load_manifest(bad_yaml)

    def test_non_mapping_yaml_raises(self, tmp_path: Path) -> None:
        bad = tmp_path / "list.yaml"
        bad.write_text("- item1\n- item2\n")
        with pytest.raises(ValueError, match="YAML mapping"):
            load_manifest(bad)


class TestSchemaValidators:
    def _make_yaml(self, tmp_path: Path, overrides: dict) -> Path:
        data = {
            "apiVersion": "syncit/v1",
            "kind": "Bundle",
            "metadata": {"name": "x", "version": "1.0"},
            "spec": {
                "targets": {"distro": "ubuntu", "codename": "noble", "arch": "amd64"},
                "tasks": [],
            },
        }
        data.update(overrides)
        p = tmp_path / "test.yaml"
        p.write_text(yaml.dump(data))
        return p

    def test_invalid_api_version_raises(self, tmp_path: Path) -> None:
        p = self._make_yaml(tmp_path, {"apiVersion": "v2"})
        with pytest.raises(ValueError, match="apiVersion"):
            load_manifest(p)

    def test_invalid_kind_raises(self, tmp_path: Path) -> None:
        p = self._make_yaml(tmp_path, {"kind": "Role"})
        with pytest.raises(ValueError, match="kind"):
            load_manifest(p)

    def test_missing_metadata_name_raises(self, tmp_path: Path) -> None:
        data = {
            "apiVersion": "syncit/v1",
            "kind": "Bundle",
            "metadata": {"version": "1.0"},
            "spec": {
                "targets": {"distro": "ubuntu", "codename": "noble", "arch": "amd64"},
                "tasks": [],
            },
        }
        p = tmp_path / "test.yaml"
        p.write_text(yaml.dump(data))
        with pytest.raises(ValueError):
            load_manifest(p)

    def test_task_spec_config_contains_extra_keys(self) -> None:
        task = TaskSpec.from_yaml_task(
            {"name": "my task", "plugin": "apt", "packages": ["git"], "custom_key": "val"}
        )
        assert task.config["packages"] == ["git"]
        assert task.config["custom_key"] == "val"
