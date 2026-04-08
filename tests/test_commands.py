"""Tests for CLI commands."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

from typer.testing import CliRunner

from offlinectl.bundle.bundle import BundleMetadata, write_meta
from offlinectl.main import app

runner = CliRunner()


def test_main_version() -> None:
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert "offlinectl" in result.stdout


def test_validate_valid_manifest(bundle_yaml: Path) -> None:
    with patch("offlinectl.plugins.oci_image._has_cmd", return_value=True):
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


def test_pack_archive_format(bundle_yaml: Path, tmp_path: Path) -> None:
    out_dir = tmp_path / "bundles"

    with patch("offlinectl.plugins.apt.AptPlugin.pack") as mock_pack:
        from offlinectl.plugins.base import PluginResult

        mock_pack.return_value = PluginResult(
            success=True, message="mocked", artifacts=[], errors=[]
        )
        with patch("offlinectl.plugins.apt.AptPlugin.validate", return_value=[]):
            result = runner.invoke(
                app,
                [
                    "pack",
                    str(bundle_yaml),
                    "--output",
                    str(out_dir),
                    "--format",
                    "tar.gz",
                    "--only",
                    "apt",
                ],
            )
    assert result.exit_code == 0
    assert "Archive ready:" in result.stdout
    # Test if it produced the file
    archives = list(out_dir.glob("*.tar.gz"))
    assert len(archives) == 1


def test_apply_archive_format(fixture_dir: Path, tmp_path: Path) -> None:
    bundle_dir = tmp_path / "bundle"
    _setup_mock_bundle(bundle_dir, fixture_dir.joinpath("bundle.yaml").read_text())

    # Now let's compress it to tar.gz manually so we can test apply detecting it
    from offlinectl.bundle.archive import pack_archive

    archive_path = pack_archive(bundle_dir, tmp_path / "test-archive", "tar.gz")

    with patch("offlinectl.plugins.oci_image._detect_runtime", return_value="docker"):
        result = runner.invoke(app, ["apply", str(archive_path), "--dry-run"])

    assert result.exit_code == 0
    assert "dry-run mode" in result.stdout


def test_transfer_command_single_host(tmp_path: Path) -> None:

    inv_file = tmp_path / "inv.yaml"
    inv_file.write_text("hosts:\n  h1:\n    host: 10.0.0.1\n    user: root\n    bundle_dest: /opt")
    bundle = tmp_path / "b.tar.gz"
    bundle.touch()

    with patch("subprocess.run") as mock_run:
        mock_run.return_value.returncode = 0
        with patch("shutil.which", return_value="scp"):
            result = runner.invoke(app, ["transfer", str(bundle), "-i", str(inv_file), "-t", "h1"])

    assert result.exit_code == 0
    mock_run.assert_called_once()
    args = mock_run.call_args[0][0]
    assert args[0] == "scp"
    assert args[-2] == str(bundle)
    assert args[-1] == "root@10.0.0.1:/opt"
    assert "-i" not in args


def test_transfer_command_with_ssh_key_and_group(tmp_path: Path) -> None:
    from unittest.mock import MagicMock

    inv_file = tmp_path / "inv.yaml"
    inv_file.write_text(
        "hosts:\n  h1:\n    host: 10.0.0.1\n    user: root\n    ssh_key: /tmp/key\n    bundle_dest: /opt\n  h2:\n    host: 10.0.0.2\n    user: r2\n    bundle_dest: /opt2\ngroups:\n  all: [h1, h2]"
    )
    bundle = tmp_path / "b.tar.gz"
    bundle.touch()

    with patch("subprocess.run") as mock_run:
        # First call succeeds, second fails
        mock_run.side_effect = [
            MagicMock(returncode=0, stderr="", stdout=""),
            MagicMock(returncode=1, stderr="fail", stdout=""),
        ]
        with patch("shutil.which", return_value="scp"):
            result = runner.invoke(app, ["transfer", str(bundle), "-i", str(inv_file), "-t", "all"])

    assert result.exit_code == 1
    assert mock_run.call_count == 2
    args_h1 = mock_run.call_args_list[0][0][0]
    assert "-i" in args_h1
    assert "/tmp/key" in args_h1
    assert args_h1[-1] == "root@10.0.0.1:/opt"

    args_h2 = mock_run.call_args_list[1][0][0]
    assert "-i" not in args_h2
    assert args_h2[-1] == "r2@10.0.0.2:/opt2"


def test_apply_remote_command(tmp_path: Path) -> None:
    inv_file = tmp_path / "inv.yaml"
    inv_file.write_text(
        "hosts:\n  h1:\n    host: 10.0.0.1\n    user: root\n    state_file: /s.json\n    bundle_dest: /opt"
    )

    with patch("subprocess.run") as mock_run:
        mock_run.return_value.returncode = 0
        mock_run.return_value.stdout = "Applied successfully"
        mock_run.return_value.stderr = ""
        with patch("shutil.which", return_value="ssh"):
            result = runner.invoke(
                app, ["apply-remote", "--bundle", "b.tar.gz", "-i", str(inv_file), "-t", "h1"]
            )

    assert result.exit_code == 0
    mock_run.assert_called_once()
    args = mock_run.call_args[0][0]
    assert args[0] == "ssh"
    assert args[1] == "root@10.0.0.1"
    assert args[2] == "offlinectl apply /opt/b.tar.gz --state-file /s.json"
