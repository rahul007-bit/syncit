"""Tests for the oci_image plugin."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from syncit.plugins.base import ApplyContext, PackContext
from syncit.plugins.oci_image import OciImagePlugin, _safe_name


@pytest.fixture
def plugin() -> OciImagePlugin:
    return OciImagePlugin()


@pytest.fixture
def pack_ctx(tmp_bundle_dir: Path) -> PackContext:
    return PackContext(bundle_dir=tmp_bundle_dir, manifest_dir=tmp_bundle_dir, dry_run=False, task_slug="images")


@pytest.fixture
def apply_ctx(tmp_bundle_dir: Path, tmp_state_file: Path) -> ApplyContext:
    return ApplyContext(bundle_dir=tmp_bundle_dir, state_file=tmp_state_file, dry_run=False, task_slug="images")


SAMPLE_IMAGES = [
    {"source": "docker.io/library/alpine:latest"},
    {"source": "docker.io/library/postgres:16-alpine"},
]


# --------------------------------------------------------------------------- #
# _safe_name()                                                                   #
# --------------------------------------------------------------------------- #


class TestSafeName:
    def test_replaces_slashes_and_colons(self) -> None:
        assert _safe_name("docker.io/library/redis:7-alpine") == "docker.io_library_redis_7-alpine"

    def test_simple_image(self) -> None:
        assert _safe_name("alpine:latest") == "alpine_latest"

    def test_no_special_chars_unchanged(self) -> None:
        assert _safe_name("alpine") == "alpine"


# --------------------------------------------------------------------------- #
# validate()                                                                    #
# --------------------------------------------------------------------------- #


class TestOciValidate:
    def test_valid_spec_with_skopeo_present(self, plugin: OciImagePlugin) -> None:
        with patch("syncit.plugins.oci_image.shutil.which", return_value="/usr/bin/skopeo"):
            errors = plugin.validate({"images": [{"source": "alpine:latest"}]})
        assert errors == []

    def test_missing_images_returns_error(self, plugin: OciImagePlugin) -> None:
        with patch("syncit.plugins.oci_image.shutil.which", return_value="/usr/bin/skopeo"):
            errors = plugin.validate({})
        assert any("images" in e for e in errors)

    def test_image_missing_source_returns_error(self, plugin: OciImagePlugin) -> None:
        with patch("syncit.plugins.oci_image.shutil.which", return_value="/usr/bin/skopeo"):
            errors = plugin.validate({"images": [{"name": "alpine"}]})
        assert any("source" in e.lower() for e in errors)

    def test_skopeo_missing_returns_error(self, plugin: OciImagePlugin) -> None:
        with patch("syncit.plugins.oci_image.shutil.which", return_value=None):
            errors = plugin.validate({"images": [{"source": "alpine:latest"}]})
        assert any("skopeo" in e.lower() for e in errors)


# --------------------------------------------------------------------------- #
# pack()                                                                        #
# --------------------------------------------------------------------------- #


class TestOciPack:
    def test_dry_run_returns_success(self, plugin: OciImagePlugin, pack_ctx: PackContext) -> None:
        pack_ctx.dry_run = True
        result = plugin.pack({"images": SAMPLE_IMAGES}, pack_ctx)
        assert result.success is True
        assert "dry-run" in result.message.lower()

    def test_skopeo_not_found_returns_failure(
        self, plugin: OciImagePlugin, pack_ctx: PackContext
    ) -> None:
        with patch("syncit.plugins.oci_image._has_cmd", return_value=False):
            result = plugin.pack({"images": SAMPLE_IMAGES}, pack_ctx)
        assert result.success is False
        assert "skopeo" in result.errors[0].lower()

    def test_pack_calls_skopeo_copy_for_each_image(
        self, plugin: OciImagePlugin, pack_ctx: PackContext
    ) -> None:
        mock_ok = MagicMock(returncode=0, stdout='{"Digest": "sha256:abc"}', stderr="")

        with patch("syncit.plugins.oci_image._has_cmd", return_value=True):
            with patch("syncit.plugins.oci_image.subprocess.run", return_value=mock_ok):
                result = plugin.pack({"images": SAMPLE_IMAGES}, pack_ctx)

        assert result.success is True

    def test_pack_writes_manifest_json(self, plugin: OciImagePlugin, pack_ctx: PackContext) -> None:
        mock_ok = MagicMock(returncode=0, stdout='{"Digest": "sha256:abc"}', stderr="")

        with patch("syncit.plugins.oci_image._has_cmd", return_value=True):
            with patch("syncit.plugins.oci_image.subprocess.run", return_value=mock_ok):
                plugin.pack({"images": SAMPLE_IMAGES}, pack_ctx)

        manifest_file = pack_ctx.bundle_dir / "images" / "manifest.json"
        assert manifest_file.exists()
        data = json.loads(manifest_file.read_text())
        assert len(data) == 2
        assert data[0]["source"] == "docker.io/library/alpine:latest"

    def test_pack_skopeo_failure_recorded_in_errors(
        self, plugin: OciImagePlugin, pack_ctx: PackContext
    ) -> None:
        mock_fail = MagicMock(returncode=1, stdout="", stderr="timeout")

        with patch("syncit.plugins.oci_image._has_cmd", return_value=True):
            with patch("syncit.plugins.oci_image.subprocess.run", return_value=mock_fail):
                result = plugin.pack({"images": SAMPLE_IMAGES}, pack_ctx)

        assert result.success is False
        assert result.errors


# --------------------------------------------------------------------------- #
# apply()                                                                       #
# --------------------------------------------------------------------------- #


class TestOciApply:
    def _setup_bundle(self, bundle_dir: Path) -> None:
        images_dir = bundle_dir / "images"
        images_dir.mkdir(parents=True)
        manifest = [
            {"source": "docker.io/library/alpine:latest", "archive": "alpine_latest.tar", "digest": "sha256:abc"},
        ]
        (images_dir / "manifest.json").write_text(json.dumps(manifest))
        (images_dir / "alpine_latest.tar").write_bytes(b"fake tar")

    def test_dry_run_returns_success(self, plugin: OciImagePlugin, apply_ctx: ApplyContext) -> None:
        self._setup_bundle(apply_ctx.bundle_dir)
        apply_ctx.dry_run = True
        with patch("syncit.plugins.oci_image._detect_runtime", return_value="docker"):
            result = plugin.apply({"images": [{"source": "docker.io/library/alpine:latest"}]}, apply_ctx)
        assert result.success is True
        assert "dry-run" in result.message.lower()

    def test_missing_manifest_returns_failure(
        self, plugin: OciImagePlugin, apply_ctx: ApplyContext
    ) -> None:
        result = plugin.apply({}, apply_ctx)
        assert result.success is False
        assert result.errors

    def test_no_runtime_detected_returns_failure(
        self, plugin: OciImagePlugin, apply_ctx: ApplyContext
    ) -> None:
        self._setup_bundle(apply_ctx.bundle_dir)
        with patch("syncit.plugins.oci_image._detect_runtime", return_value=None):
            result = plugin.apply({}, apply_ctx)
        assert result.success is False
        assert len(result.errors) > 0

    def test_apply_calls_docker_load(self, plugin: OciImagePlugin, apply_ctx: ApplyContext) -> None:
        self._setup_bundle(apply_ctx.bundle_dir)
        mock_ok = MagicMock(returncode=0, stdout="", stderr="")

        with patch("syncit.plugins.oci_image._detect_runtime", return_value="docker"):
            with patch("syncit.plugins.oci_image._run", return_value=mock_ok):
                with patch.object(plugin, "_image_exists", side_effect=[False, True]):
                    result = plugin.apply({}, apply_ctx)

        assert result.success is True

    def test_apply_runtime_podman_uses_podman_load(
        self, plugin: OciImagePlugin, apply_ctx: ApplyContext
    ) -> None:
        self._setup_bundle(apply_ctx.bundle_dir)
        mock_ok = MagicMock(returncode=0, stdout="", stderr="")
        captured_cmds = []

        def capture_run(cmd, **kwargs):
            captured_cmds.append(cmd)
            return mock_ok

        with patch("syncit.plugins.oci_image._detect_runtime", return_value="podman"):
            with patch("syncit.plugins.oci_image._run", side_effect=capture_run):
                with patch.object(plugin, "_image_exists", side_effect=[False, True]):
                    plugin.apply({}, apply_ctx)

        assert any("podman" in cmd[0] for cmd in captured_cmds)

    def test_idempotence_skips_existing_image(
        self, plugin: OciImagePlugin, apply_ctx: ApplyContext
    ) -> None:
        self._setup_bundle(apply_ctx.bundle_dir)
        apply_ctx.verbose = True

        with patch("syncit.plugins.oci_image._detect_runtime", return_value="docker"):
            with patch("syncit.plugins.oci_image._run") as mock_run:
                with patch.object(plugin, "_image_exists", return_value=True):
                    result = plugin.apply({}, apply_ctx)

        # _run (docker load) should NOT have been called for the image
        load_calls = [c for c in mock_run.call_args_list if c.args and "load" in str(c.args[0])]
        assert load_calls == []
        assert result.success is True


# --------------------------------------------------------------------------- #
# diff()                                                                        #
# --------------------------------------------------------------------------- #


class TestOciDiff:
    def test_diff_new_task_marks_all_added(self, plugin: OciImagePlugin) -> None:
        result = plugin.diff(None, {"images": SAMPLE_IMAGES})
        assert set(result.added) == {"docker.io/library/alpine:latest", "docker.io/library/postgres:16-alpine"}
        assert result.removed == []

    def test_diff_added_image(self, plugin: OciImagePlugin) -> None:
        result = plugin.diff(
            {"images": [{"source": "docker.io/library/alpine:latest"}]},
            {"images": SAMPLE_IMAGES},
        )
        assert "docker.io/library/postgres:16-alpine" in result.added
        assert "docker.io/library/alpine:latest" in result.unchanged

    def test_diff_removed_image(self, plugin: OciImagePlugin) -> None:
        result = plugin.diff(
            {"images": SAMPLE_IMAGES},
            {"images": [{"source": "docker.io/library/alpine:latest"}]},
        )
        assert "docker.io/library/postgres:16-alpine" in result.removed
        assert "docker.io/library/alpine:latest" in result.unchanged

    def test_diff_no_changes(self, plugin: OciImagePlugin) -> None:
        spec = {"images": SAMPLE_IMAGES}
        result = plugin.diff(spec, spec)
        assert result.added == []
        assert result.removed == []
        assert len(result.unchanged) == 2
