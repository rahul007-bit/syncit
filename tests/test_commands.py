"""Tests for CLI commands."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

from typer.testing import CliRunner

from offlinectl.bundle.bundle import BundleMetadata, write_meta
from offlinectl.main import app

runner = CliRunner()


def test_main_version() -> None:
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert "offlinectl" in result.stdout


def test_validate_valid_manifest(bundle_yaml: Path) -> None:
    # Ensure offlinectl/plugins/oci_image._has_cmd returns True to pass validation step if it executes it
    with patch("offlinectl.plugins.oci_image._has_cmd", return_value=True):
        result = runner.invoke(app, ["validate", str(bundle_yaml)])
    assert result.exit_code == 0
    assert "OK" in result.stdout


def test_validate_missing_manifest(tmp_path: Path) -> None:
    result = runner.invoke(app, ["validate", str(tmp_path / "missing.yaml")])
    assert result.exit_code == 1


def test_validate_skips_unknown_plugin(fixture_dir: Path, tmp_path: Path) -> None:
    man = tmp_path / "bundle.yaml"
    man.write_text(
        fixture_dir.joinpath("bundle.yaml")
        .read_text()
        .replace("plugin: apt", "plugin: unknown_plugin")
    )
    result = runner.invoke(app, ["validate", str(man)])
    assert result.exit_code == 1


def test_pack_dry_run(bundle_yaml: Path, tmp_path: Path) -> None:
    out_dir = tmp_path / "bundles"
    # Ensure offlinectl/plugins/oci_image._has_cmd returns True to pass validation step if it executes it
    with patch("offlinectl.plugins.oci_image._has_cmd", return_value=True):
        result = runner.invoke(
            app, ["pack", str(bundle_yaml), "--output", str(out_dir), "--dry-run"]
        )
    assert result.exit_code == 0
    assert "dry-run mode" in result.stdout
    assert "Would write bundle to:" in result.stdout


def test_pack_missing_manifest(tmp_path: Path) -> None:
    result = runner.invoke(app, ["pack", str(tmp_path / "missing.yaml")])
    assert result.exit_code == 1


def test_pack_unknown_plugin(fixture_dir: Path, tmp_path: Path) -> None:
    man = tmp_path / "bundle.yaml"
    man.write_text(
        fixture_dir.joinpath("bundle.yaml")
        .read_text()
        .replace("plugin: apt", "plugin: unknown_plugin")
    )
    result = runner.invoke(app, ["pack", str(man), "--output", str(tmp_path), "--dry-run"])
    assert result.exit_code == 1


def test_pack_continue_on_error(fixture_dir: Path, tmp_path: Path) -> None:
    # Actually `--continue-on-error` doesn't exist natively on pack, but failures normally raise typer.Exit(1).
    # Pack returns `result.success` from plugin.pack
    # We can mock plugin 'apt' to return a failed PluginResult
    with patch("offlinectl.plugins.apt.AptPlugin.pack") as mock_pack:
        from offlinectl.plugins.base import PluginResult

        mock_pack.return_value = PluginResult(
            success=False, message="failed", artifacts=[], errors=["apt error"]
        )
        with patch("offlinectl.plugins.oci_image._has_cmd", return_value=True):
            result = runner.invoke(
                app, ["pack", str(fixture_dir / "bundle.yaml"), "--output", str(tmp_path)]
            )
    assert result.exit_code == 1


def _setup_mock_bundle(bundle_dir: Path, manifest_content: str) -> None:
    import json

    bundle_dir.mkdir(parents=True, exist_ok=True)
    (bundle_dir / "bundle.yaml").write_text(manifest_content)

    # apt artifacts
    apt_dir = bundle_dir / "apt"
    (apt_dir / "debs").mkdir(parents=True)
    (apt_dir / "Packages").write_text("Package: git\nVersion: 1:2.43\n")

    # pip artifacts
    wheels = bundle_dir / "pip" / "wheels"
    wheels.mkdir(parents=True)
    (bundle_dir / "pip" / "requirements.txt").write_text("requests==2.31.0\n")

    # oci_image artifacts
    images_dir = bundle_dir / "images"
    images_dir.mkdir(parents=True)
    manifest = [{"source": "alpine:latest", "archive": "alpine_latest.tar"}]
    (images_dir / "manifest.json").write_text(json.dumps(manifest))
    (images_dir / "alpine_latest.tar").write_bytes(b"fake tar")

    meta = BundleMetadata(
        name="test",
        version="1.0",
        created_at=datetime.now(UTC),
        offlinectl_version="0.1",
        targets={"distro": "u", "codename": "c", "arch": "a"},
        tasks=[],
    )
    write_meta(bundle_dir, meta)


def test_apply_dry_run(fixture_dir: Path, tmp_path: Path) -> None:
    bundle_dir = tmp_path / "bundle"
    _setup_mock_bundle(bundle_dir, fixture_dir.joinpath("bundle.yaml").read_text())

    with patch("offlinectl.plugins.oci_image._detect_runtime", return_value="docker"):
        result = runner.invoke(app, ["apply", str(bundle_dir), "--dry-run"])
    assert result.exit_code == 0
    assert "dry-run mode" in result.stdout


def test_apply_missing_bundle(tmp_path: Path) -> None:
    result = runner.invoke(app, ["apply", str(tmp_path / "nonexistent")])
    assert result.exit_code == 1


def test_apply_missing_meta(fixture_dir: Path, tmp_path: Path) -> None:
    bundle_dir = tmp_path / "bundle"
    bundle_dir.mkdir()
    result = runner.invoke(app, ["apply", str(bundle_dir)])
    assert result.exit_code == 1


def test_apply_unknown_plugin(fixture_dir: Path, tmp_path: Path) -> None:
    bundle_dir = tmp_path / "bundle"
    content = (
        fixture_dir.joinpath("bundle.yaml")
        .read_text()
        .replace("plugin: apt", "plugin: unknown_plugin")
    )
    _setup_mock_bundle(bundle_dir, content)
    result = runner.invoke(app, ["apply", str(bundle_dir)])
    assert result.exit_code == 1


def test_apply_with_force_and_only(fixture_dir: Path, tmp_path: Path) -> None:
    bundle_dir = tmp_path / "bundle"
    _setup_mock_bundle(bundle_dir, fixture_dir.joinpath("bundle.yaml").read_text())

    with patch("offlinectl.plugins.oci_image._detect_runtime", return_value="docker"):
        result = runner.invoke(
            app, ["apply", str(bundle_dir), "--dry-run", "--force", "--only", "apt"]
        )
    assert result.exit_code == 0
    assert "Skipping" in result.stdout  # pip and oci_image skipped
    assert "apt" in result.stdout


def test_diff_success(fixture_dir: Path, tmp_path: Path) -> None:
    b1 = tmp_path / "b1"
    b2 = tmp_path / "b2"
    _setup_mock_bundle(b1, fixture_dir.joinpath("bundle.yaml").read_text())

    mod_content = (
        fixture_dir.joinpath("bundle.yaml")
        .read_text()
        .replace('python_version: "3.11"', 'python_version: "3.12"')
    )
    _setup_mock_bundle(b2, mod_content)

    result = runner.invoke(app, ["diff", str(b1), str(b2)])
    assert result.exit_code == 0
    assert "~" in result.stdout or "Identical" in result.stdout or "Diffing" in result.stdout


def test_diff_missing_bundle(tmp_path: Path) -> None:
    result = runner.invoke(app, ["diff", str(tmp_path / "n1"), str(tmp_path / "n2")])
    assert result.exit_code == 1


def test_diff_unknown_plugin(fixture_dir: Path, tmp_path: Path) -> None:
    b1 = tmp_path / "b1"
    b2 = tmp_path / "b2"
    _setup_mock_bundle(b1, fixture_dir.joinpath("bundle.yaml").read_text())

    mod_content = (
        fixture_dir.joinpath("bundle.yaml")
        .read_text()
        .replace("plugin: apt", "plugin: unknown_plugin")
    )
    _setup_mock_bundle(b2, mod_content)

    result = runner.invoke(app, ["diff", str(b1), str(b2)])
    assert result.exit_code == 0
