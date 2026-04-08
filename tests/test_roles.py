"""Tests for the roles system: schema, loader, and expander."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from offlinectl.manifest.loader import load_manifest
from offlinectl.manifest.schema import BundleManifest
from offlinectl.roles.expander import expand_roles
from offlinectl.roles.loader import load_role


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_role_dir(tmp_path: Path, name: str, content: dict) -> Path:
    """Create a role directory with a role.yaml and return its path."""
    role_dir = tmp_path / name
    role_dir.mkdir(parents=True, exist_ok=True)
    (role_dir / "role.yaml").write_text(yaml.dump(content))
    return role_dir


def make_manifest(tmp_path: Path, spec_overlay: dict | None = None) -> tuple[Path, BundleManifest]:
    """Create a minimal bundle.yaml and return (path, manifest)."""
    data: dict = {
        "apiVersion": "offlinectl/v1",
        "kind": "Bundle",
        "metadata": {"name": "test-bundle", "version": "1.0.0"},
        "spec": {
            "targets": {"distro": "ubuntu", "codename": "jammy", "arch": "amd64"},
            "tasks": [],
        },
    }
    if spec_overlay:
        data["spec"].update(spec_overlay)
    bundle_path = tmp_path / "bundle.yaml"
    bundle_path.write_text(yaml.dump(data))
    return bundle_path, BundleManifest(**data)


# ---------------------------------------------------------------------------
# load_role
# ---------------------------------------------------------------------------


class TestLoadRole:
    def test_valid_role(self, tmp_path: Path) -> None:
        role_dir = make_role_dir(
            tmp_path,
            "monitoring",
            {
                "name": "monitoring",
                "description": "Prometheus stack",
                "version": "1.0.0",
                "tasks": [
                    {
                        "plugin": "oci_image",
                        "name": "prometheus",
                        "spec": {"images": [{"source": "quay.io/prometheus/prometheus:v2.51"}]},
                    }
                ],
            },
        )
        role = load_role(role_dir)
        assert role.name == "monitoring"
        assert role.version == "1.0.0"
        assert len(role.tasks) == 1
        assert role.tasks[0].plugin == "oci_image"
        assert role.tasks[0].name == "prometheus"

    def test_missing_role_path(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError, match="does not exist"):
            load_role(tmp_path / "nonexistent")

    def test_missing_role_yaml(self, tmp_path: Path) -> None:
        role_dir = tmp_path / "empty-role"
        role_dir.mkdir()
        with pytest.raises(FileNotFoundError, match="role.yaml not found"):
            load_role(role_dir)

    def test_invalid_schema_missing_name(self, tmp_path: Path) -> None:
        role_dir = tmp_path / "bad-role"
        role_dir.mkdir()
        (role_dir / "role.yaml").write_text(yaml.dump({"tasks": []}))
        with pytest.raises(ValueError, match="Role validation failed"):
            load_role(role_dir)

    def test_invalid_yaml(self, tmp_path: Path) -> None:
        role_dir = tmp_path / "bad-yaml"
        role_dir.mkdir()
        (role_dir / "role.yaml").write_text("key: [unclosed")
        with pytest.raises(ValueError, match="Failed to parse"):
            load_role(role_dir)

    def test_role_without_tasks(self, tmp_path: Path) -> None:
        role_dir = make_role_dir(tmp_path, "empty", {"name": "empty"})
        role = load_role(role_dir)
        assert role.tasks == []


# ---------------------------------------------------------------------------
# expand_roles
# ---------------------------------------------------------------------------


class TestExpandRoles:
    def test_no_roles_is_noop(self, tmp_path: Path) -> None:
        bundle_path, manifest = make_manifest(
            tmp_path,
            {"tasks": [{"plugin": "apt", "name": "base", "packages": ["git"]}]},
        )
        result = expand_roles(manifest, tmp_path)
        assert len(result.spec.tasks) == 1
        assert result.spec.tasks[0]["name"] == "base"

    def test_role_tasks_come_first(self, tmp_path: Path) -> None:
        # Create a role with one task
        role_dir = make_role_dir(
            tmp_path,
            "roles/monitoring",
            {
                "name": "monitoring",
                "tasks": [{"plugin": "oci_image", "name": "prometheus", "spec": {}}],
            },
        )

        bundle_path, manifest = make_manifest(
            tmp_path,
            {
                "roles": [{"path": "./roles/monitoring"}],
                "tasks": [{"plugin": "apt", "name": "base", "packages": ["git"]}],
            },
        )
        result = expand_roles(manifest, tmp_path)

        assert len(result.spec.tasks) == 2
        assert result.spec.tasks[0]["name"] == "prometheus"  # role task first
        assert result.spec.tasks[1]["name"] == "base"  # inline task second

    def test_multiple_roles_merged_in_order(self, tmp_path: Path) -> None:
        make_role_dir(
            tmp_path,
            "roles/role-a",
            {"name": "role-a", "tasks": [{"plugin": "apt", "name": "task-a", "spec": {}}]},
        )
        make_role_dir(
            tmp_path,
            "roles/role-b",
            {"name": "role-b", "tasks": [{"plugin": "apt", "name": "task-b", "spec": {}}]},
        )

        bundle_path, manifest = make_manifest(
            tmp_path,
            {"roles": [{"path": "./roles/role-a"}, {"path": "./roles/role-b"}]},
        )
        result = expand_roles(manifest, tmp_path)

        names = [t["name"] for t in result.spec.tasks]
        assert names == ["task-a", "task-b"]

    def test_duplicate_task_name_raises(self, tmp_path: Path) -> None:
        make_role_dir(
            tmp_path,
            "roles/role-a",
            {"name": "role-a", "tasks": [{"plugin": "apt", "name": "duplicate", "spec": {}}]},
        )

        bundle_path, manifest = make_manifest(
            tmp_path,
            {
                "roles": [{"path": "./roles/role-a"}],
                "tasks": [{"plugin": "apt", "name": "duplicate", "packages": ["git"]}],
            },
        )
        with pytest.raises(ValueError, match="Duplicate task"):
            expand_roles(manifest, tmp_path)

    def test_duplicate_across_roles_raises(self, tmp_path: Path) -> None:
        make_role_dir(
            tmp_path,
            "roles/role-a",
            {"name": "role-a", "tasks": [{"plugin": "apt", "name": "shared", "spec": {}}]},
        )
        make_role_dir(
            tmp_path,
            "roles/role-b",
            {"name": "role-b", "tasks": [{"plugin": "apt", "name": "shared", "spec": {}}]},
        )

        bundle_path, manifest = make_manifest(
            tmp_path,
            {"roles": [{"path": "./roles/role-a"}, {"path": "./roles/role-b"}]},
        )
        with pytest.raises(ValueError, match="Duplicate task"):
            expand_roles(manifest, tmp_path)

    def test_missing_role_path_raises(self, tmp_path: Path) -> None:
        bundle_path, manifest = make_manifest(
            tmp_path,
            {"roles": [{"path": "./roles/nonexistent"}]},
        )
        with pytest.raises(FileNotFoundError, match="does not exist"):
            expand_roles(manifest, tmp_path)


# ---------------------------------------------------------------------------
# Integration: load_manifest with roles
# ---------------------------------------------------------------------------


class TestLoadManifestWithRoles:
    def test_roles_expanded_on_load(self, tmp_path: Path) -> None:
        make_role_dir(
            tmp_path,
            "roles/base",
            {
                "name": "base",
                "tasks": [
                    {
                        "plugin": "apt",
                        "name": "base-packages",
                        "spec": {"packages": ["curl", "git"]},
                    }
                ],
            },
        )

        data = {
            "apiVersion": "offlinectl/v1",
            "kind": "Bundle",
            "metadata": {"name": "test", "version": "1.0.0"},
            "spec": {
                "targets": {"distro": "ubuntu", "codename": "jammy", "arch": "amd64"},
                "roles": [{"path": "./roles/base"}],
                "tasks": [{"plugin": "pip", "name": "app-deps", "packages": ["flask"]}],
            },
        }
        bundle_path = tmp_path / "bundle.yaml"
        bundle_path.write_text(yaml.dump(data))

        manifest = load_manifest(bundle_path)
        task_names = [t["name"] for t in manifest.spec.tasks]
        assert task_names == ["base-packages", "app-deps"]

    def test_load_manifest_without_roles_unchanged(self, tmp_path: Path) -> None:
        data = {
            "apiVersion": "offlinectl/v1",
            "kind": "Bundle",
            "metadata": {"name": "test", "version": "1.0.0"},
            "spec": {
                "targets": {"distro": "ubuntu", "codename": "jammy", "arch": "amd64"},
                "tasks": [{"plugin": "apt", "name": "curl", "packages": ["curl"]}],
            },
        }
        bundle_path = tmp_path / "bundle.yaml"
        bundle_path.write_text(yaml.dump(data))

        manifest = load_manifest(bundle_path)
        assert len(manifest.spec.tasks) == 1
        assert manifest.spec.tasks[0]["name"] == "curl"
