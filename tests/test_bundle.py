"""Tests for bundle metadata and state management."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from syncit.bundle.bundle import (
    BundleMetadata,
    BundleTaskMeta,
    compute_task_checksum,
    read_meta,
    write_meta,
)
from syncit.bundle.state import AppliedTask, BundleState, load_state, save_state


class TestBundleMetadata:
    def test_write_and_read_roundtrip(self, tmp_bundle_dir: Path) -> None:
        meta = BundleMetadata(
            name="my-bundle",
            version="2025.04.0",
            created_at=datetime(2025, 4, 7, 12, 0, 0, tzinfo=UTC),
            syncit_version="0.1.0",
            targets={"distro": "ubuntu", "codename": "noble", "arch": "amd64"},
            tasks=[
                BundleTaskMeta(
                    name="Install packages", plugin="apt", status="packed", artifact_count=5
                )
            ],
        )
        write_meta(tmp_bundle_dir, meta)

        loaded = read_meta(tmp_bundle_dir)
        assert loaded.name == "my-bundle"
        assert loaded.version == "2025.04.0"
        assert loaded.syncit_version == "0.1.0"
        assert loaded.targets["distro"] == "ubuntu"
        assert len(loaded.tasks) == 1
        assert loaded.tasks[0].plugin == "apt"

    def test_read_meta_missing_file(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            read_meta(tmp_path / "empty_dir")

    def test_meta_file_written_as_json(self, tmp_bundle_dir: Path) -> None:
        meta = BundleMetadata(
            name="x",
            version="1.0",
            created_at=datetime(2025, 1, 1, tzinfo=UTC),
            syncit_version="0.1.0",
            targets={},
            tasks=[],
        )
        write_meta(tmp_bundle_dir, meta)
        raw = json.loads((tmp_bundle_dir / "bundle.meta.json").read_text())
        assert raw["name"] == "x"


class TestBundleState:
    def test_empty_state_defaults(self) -> None:
        state = BundleState()
        assert state.last_bundle is None
        assert state.applied_tasks == []

    def test_load_state_missing_file_returns_empty(self, tmp_state_file: Path) -> None:
        state = load_state(tmp_state_file)
        assert isinstance(state, BundleState)
        assert state.last_bundle is None

    def test_save_and_load_roundtrip(self, tmp_state_file: Path) -> None:
        state = BundleState(last_bundle="my-bundle-1.0")
        task = AppliedTask(
            name="Install packages",
            plugin="apt",
            bundle_version="1.0",
            applied_at=datetime(2025, 4, 7, tzinfo=UTC),
            checksum="sha256:abc",
        )
        state.upsert_task(task)
        save_state(tmp_state_file, state)

        loaded = load_state(tmp_state_file)
        assert loaded.last_bundle == "my-bundle-1.0"
        assert len(loaded.applied_tasks) == 1
        assert loaded.applied_tasks[0].checksum == "sha256:abc"

    def test_upsert_replaces_existing(self) -> None:
        state = BundleState()
        t1 = AppliedTask(
            name="task-a",
            plugin="apt",
            bundle_version="1.0",
            applied_at=datetime(2025, 1, 1, tzinfo=UTC),
            checksum="sha256:old",
        )
        t2 = AppliedTask(
            name="task-a",
            plugin="apt",
            bundle_version="2.0",
            applied_at=datetime(2025, 2, 1, tzinfo=UTC),
            checksum="sha256:new",
        )
        state.upsert_task(t1)
        state.upsert_task(t2)

        assert len(state.applied_tasks) == 1
        assert state.applied_tasks[0].checksum == "sha256:new"

    def test_get_task_returns_none_for_missing(self) -> None:
        state = BundleState()
        assert state.get_task("nonexistent") is None

    def test_save_creates_parent_dirs(self, tmp_path: Path) -> None:
        deep_path = tmp_path / "a" / "b" / "state.json"
        save_state(deep_path, BundleState())
        assert deep_path.exists()


class TestComputeChecksum:
    def test_empty_dir_returns_empty_hash(self, tmp_bundle_dir: Path) -> None:
        result = compute_task_checksum(tmp_bundle_dir, "apt")
        assert result == "sha256:"

    def test_same_files_same_checksum(self, tmp_bundle_dir: Path) -> None:
        apt_dir = tmp_bundle_dir / "apt"
        apt_dir.mkdir()
        (apt_dir / "Packages").write_text("Package: git\n")

        c1 = compute_task_checksum(tmp_bundle_dir, "apt")
        c2 = compute_task_checksum(tmp_bundle_dir, "apt")
        assert c1 == c2
        assert c1.startswith("sha256:")

    def test_different_files_different_checksum(self, tmp_bundle_dir: Path) -> None:
        apt_dir = tmp_bundle_dir / "apt"
        apt_dir.mkdir()
        (apt_dir / "Packages").write_text("Package: git\n")
        c1 = compute_task_checksum(tmp_bundle_dir, "apt")

        (apt_dir / "Packages").write_text("Package: curl\n")
        c2 = compute_task_checksum(tmp_bundle_dir, "apt")
        assert c1 != c2
