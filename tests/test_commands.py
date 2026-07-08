"""Tests for CLI commands."""

from __future__ import annotations

import subprocess
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

from typer.testing import CliRunner

from syncit.bundle.bundle import BundleMetadata, write_meta
from syncit.main import app

runner = CliRunner()


def test_main_version() -> None:
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert "syncit" in result.stdout


def test_validate_valid_manifest(bundle_yaml: Path) -> None:
    with patch("syncit.plugins.oci_image._has_cmd", return_value=True):
        with patch("shutil.which", return_value="fake_path"):
            with patch("subprocess.run") as mock_run:
                mock_run.return_value = MagicMock(returncode=0)
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
    # Ensure syncit/plugins/oci_image._has_cmd returns True to pass validation step if it executes it
    with patch("syncit.plugins.oci_image._has_cmd", return_value=True):
        with patch("shutil.which", return_value="fake_path"):
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
    with patch("syncit.plugins.apt.AptPlugin.pack") as mock_pack:
        from syncit.plugins.base import PluginResult

        mock_pack.return_value = PluginResult(
            success=False, message="failed", artifacts=[], errors=["apt error"]
        )
        with patch("syncit.plugins.oci_image._has_cmd", return_value=True):
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
        syncit_version="0.1",
        targets={"distro": "u", "codename": "c", "arch": "a"},
        tasks=[],
    )
    write_meta(bundle_dir, meta)


def test_apply_dry_run(fixture_dir: Path, tmp_path: Path) -> None:
    bundle_dir = tmp_path / "bundle"
    _setup_mock_bundle(bundle_dir, fixture_dir.joinpath("bundle.yaml").read_text())

    # Create a dummy archive so detect_bundle works
    from syncit.bundle.archive import pack_archive

    archive_path = pack_archive(bundle_dir, tmp_path / "test", "tar.gz")

    result = runner.invoke(app, ["apply", "--bundle", str(archive_path), "--print-script"])
    assert result.exit_code == 0
    assert "#!/usr/bin/env bash" in result.stdout


def test_apply_missing_bundle(tmp_path: Path) -> None:
    result = runner.invoke(app, ["apply", "--bundle", str(tmp_path / "nonexistent")])
    assert result.exit_code == 1


def test_apply_command(tmp_path: Path) -> None:
    inv_file = tmp_path / "inv.yaml"
    inv_file.write_text(
        "hosts:\n  h1:\n    host: 10.0.0.1\n    user: root\n    state_file: /s.json\n    bundle_dest: /opt"
    )

    bundle_path = tmp_path / "b.tar.gz"
    bundle_path.write_text("fake tar")

    # subprocess.run calls in order:
    # 0: SCP bundle
    # 1: SSH extract + mkdir
    # 2: SSH cat state.json -> return empty state
    # 3-5: SSH sudo tee state.json (one per task, 3 tasks in fixture)
    run_side_effects = [
        MagicMock(returncode=0),  # SCP
        MagicMock(returncode=0),  # extract
        MagicMock(returncode=1, stdout=""),  # cat state.json -> not found
        MagicMock(returncode=0),  # tee after task 1
        MagicMock(returncode=0),  # tee after task 2
        MagicMock(returncode=0),  # tee after task 3
    ]

    with patch("subprocess.run") as mock_run:
        mock_run.side_effect = run_side_effects
        with patch("subprocess.Popen") as mock_popen:
            mock_proc = MagicMock()
            mock_proc.returncode = 0
            mock_proc.stdin = MagicMock()
            mock_popen.return_value = mock_proc

            with patch("shutil.which", return_value="ssh"):
                with patch("syncit.bundle.archive.detect_bundle") as mock_detect:
                    mock_detect.return_value.__enter__.return_value = tmp_path
                    fixture_bundle = Path("tests/fixtures/bundle.yaml").read_text()
                    (tmp_path / "bundle.yaml").write_text(fixture_bundle)

                    result = runner.invoke(
                        app,
                        [
                            "apply",
                            "--bundle",
                            str(bundle_path),
                            "-i",
                            str(inv_file),
                            "-t",
                            "h1",
                        ],
                    )

    if result.exit_code != 0:
        print("EXCEPTION:", result.exception)
        print("OUTPUT:", result.output)
    assert result.exit_code == 0
    assert mock_run.call_count == 6


def test_apply_with_ssh_key(tmp_path: Path) -> None:
    """Verify ssh_key is threaded through both scp and ssh invocations."""
    inv_file = tmp_path / "inv.yaml"
    inv_file.write_text(
        "hosts:\n"
        "  h1:\n"
        "    host: 10.0.0.1\n"
        "    user: deploy\n"
        "    bundle_dest: /srv/bundles\n"
        "    ssh_key: /home/user/.ssh/id_ed25519\n"
    )
    bundle_path = tmp_path / "b.tar.gz"
    bundle_path.write_text("fake")

    run_side_effects = [
        MagicMock(returncode=0),  # SCP
        MagicMock(returncode=0),  # extract
        MagicMock(returncode=1, stdout=""),  # cat state.json -> not found
        MagicMock(returncode=0),  # tee after task 1
        MagicMock(returncode=0),  # tee after task 2
        MagicMock(returncode=0),  # tee after task 3
    ]

    with patch("subprocess.run") as mock_run:
        mock_run.side_effect = run_side_effects
        with patch("subprocess.Popen") as mock_popen:
            mock_proc = MagicMock()
            mock_proc.returncode = 0
            mock_proc.stdin = MagicMock()
            mock_popen.return_value = mock_proc

            with patch("shutil.which", return_value="ssh"):
                with patch("syncit.bundle.archive.detect_bundle") as mock_detect:
                    mock_detect.return_value.__enter__.return_value = tmp_path
                    (tmp_path / "bundle.yaml").write_text(
                        Path("tests/fixtures/bundle.yaml").read_text()
                    )
                    result = runner.invoke(
                        app,
                        [
                            "apply",
                            "--bundle",
                            str(bundle_path),
                            "-i",
                            str(inv_file),
                            "-t",
                            "h1",
                        ],
                    )

    assert result.exit_code == 0

    # Verify -i flag in SCP call
    scp_call = mock_run.call_args_list[0][0][0]
    assert "-i" in scp_call
    assert "/home/user/.ssh/id_ed25519" in scp_call


def test_apply_skips_unchanged(tmp_path: Path) -> None:
    """If remote state has matching checksum + 'success', the task is skipped and Popen is not called."""
    inv_file = tmp_path / "inv.yaml"
    inv_file.write_text(
        "hosts:\n  h1:\n    host: 10.0.0.1\n    user: root\n    state_file: /s.json\n    bundle_dest: /opt"
    )

    bundle_path = tmp_path / "b.tar.gz"
    bundle_path.write_text("fake tar")

    # Compute the actual checksums for the bundle so we can put them in the remote state
    with patch("syncit.bundle.archive.detect_bundle") as prep_detect:
        prep_detect.return_value.__enter__.return_value = tmp_path
        (tmp_path / "bundle.yaml").write_text(Path("tests/fixtures/bundle.yaml").read_text())

    from syncit.bundle.bundle import compute_task_checksum
    from syncit.state import RemoteState, TaskState

    # Build a remote state where all 3 tasks have matching checksums and "success"
    apt_checksum = compute_task_checksum(tmp_path, "apt")
    pip_checksum = compute_task_checksum(tmp_path, "pip")
    oci_checksum = compute_task_checksum(tmp_path, "oci_image")

    existing_state = RemoteState(
        applied_tasks={
            "Install base packages": TaskState(checksum=apt_checksum, status="success"),
            "Install Pip Deps": TaskState(checksum=pip_checksum, status="success"),
            "Sync Images": TaskState(checksum=oci_checksum, status="success"),
        }
    )

    # subprocess.run calls: SCP, extract, cat state.json (returns existing state)
    run_side_effects = [
        MagicMock(returncode=0),  # SCP
        MagicMock(returncode=0),  # extract
        MagicMock(returncode=0, stdout=existing_state.model_dump_json()),  # cat state.json
    ]

    with patch("subprocess.run") as mock_run:
        mock_run.side_effect = run_side_effects
        with patch("subprocess.Popen") as mock_popen:
            with patch("shutil.which", return_value="ssh"):
                with patch("syncit.bundle.archive.detect_bundle") as mock_detect:
                    mock_detect.return_value.__enter__.return_value = tmp_path
                    (tmp_path / "bundle.yaml").write_text(
                        Path("tests/fixtures/bundle.yaml").read_text()
                    )

                    result = runner.invoke(
                        app,
                        [
                            "apply",
                            "--bundle",
                            str(bundle_path),
                            "-i",
                            str(inv_file),
                            "-t",
                            "h1",
                        ],
                    )

    assert result.exit_code == 0
    mock_popen.assert_not_called()
    assert "SKIP" in result.output


def test_up_command(tmp_path: Path) -> None:
    inv_file = tmp_path / "inv.yaml"
    inv_file.write_text("hosts:\n  h1:\n    host: 10.0.0.1\n    user: root\n    bundle_dest: /opt")
    manifest = Path("tests/fixtures/bundle.yaml")

    with patch("syncit.commands.up.run_pack") as mock_pack:
        bundle_tar = tmp_path / "bundle.tar.gz"
        bundle_tar.write_text("fake")
        mock_pack.return_value = bundle_tar

        with patch("syncit.commands.up.run_apply") as mock_apply:
            result = runner.invoke(
                app,
                ["up", str(manifest), "-i", str(inv_file), "-t", "h1"],
            )

    assert result.exit_code == 0
    mock_pack.assert_called_once()
    mock_apply.assert_called_once()
    params = mock_apply.call_args[1]
    assert params["bundle_path"] == bundle_tar
    assert params["target"] == "h1"
    assert "syncit up' completed successfully" in result.stdout
