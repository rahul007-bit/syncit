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

    with patch("syncit.plugins.oci_image._detect_runtime", return_value="docker"):
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

    with patch("syncit.plugins.oci_image._detect_runtime", return_value="docker"):
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

    with patch("syncit.plugins.apt.AptPlugin.pack") as mock_pack:
        from syncit.plugins.base import PluginResult

        mock_pack.return_value = PluginResult(
            success=True, message="mocked", artifacts=[], errors=[]
        )
        with patch("syncit.plugins.apt.AptPlugin.validate", return_value=[]):
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
    from syncit.bundle.archive import pack_archive

    archive_path = pack_archive(bundle_dir, tmp_path / "test-archive", "tar.gz")

    with patch("syncit.plugins.oci_image._detect_runtime", return_value="docker"):
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
                            "apply-remote",
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

    # subprocess.run calls: SCP, extract, cat state, 3x tee state = 6 total
    assert mock_run.call_count == 6

    # Call 0: SCP — transfers bundle to remote
    scp_call = mock_run.call_args_list[0][0][0]
    assert scp_call[0] == "scp"
    assert str(bundle_path) in scp_call
    assert "root@10.0.0.1:/opt" in scp_call

    # Call 1: SSH extract + mkdir
    extract_call = mock_run.call_args_list[1][0][0]
    assert extract_call[0] == "ssh"
    assert "root@10.0.0.1" in extract_call
    extract_cmd_str = extract_call[-1]
    assert "tar -xf" in extract_cmd_str
    assert "mkdir -p /opt/syncit/" in extract_cmd_str

    # Call 2: SSH cat state.json
    cat_call = mock_run.call_args_list[2][0][0]
    assert cat_call[0] == "ssh"
    assert "sudo" in cat_call
    assert "cat" in cat_call
    assert "/s.json" in cat_call

    # Calls 3-5: SSH sudo tee state.json (one per task)
    for i in range(3, 6):
        tee_call = mock_run.call_args_list[i][0][0]
        assert tee_call[0] == "ssh"
        assert "sudo" in tee_call
        assert "tee" in tee_call
        assert "/s.json" in tee_call

    # Popen: 3 calls (one per task), each piping via "sudo bash -s"
    assert mock_popen.call_count == 3
    for popen_call in mock_popen.call_args_list:
        ssh_cmd = popen_call[0][0]
        assert ssh_cmd[0] == "ssh"
        assert "root@10.0.0.1" in ssh_cmd
        assert "sudo" in ssh_cmd
        assert "bash" in ssh_cmd
        assert "-s" in ssh_cmd
        # stdin should be a PIPE
        assert popen_call[1]["stdin"] == subprocess.PIPE


def test_apply_remote_with_ssh_key(tmp_path: Path) -> None:
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
                            "apply-remote",
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

    # Verify -i flag in Popen SSH calls
    for popen_call in mock_popen.call_args_list:
        ssh_cmd = popen_call[0][0]
        assert "-i" in ssh_cmd
        assert "/home/user/.ssh/id_ed25519" in ssh_cmd


def test_apply_remote_print_script(tmp_path: Path) -> None:
    """Verify --print-script prints the script and exits without SSH/SCP."""
    bundle_path = tmp_path / "b.tar.gz"
    bundle_path.write_text("fake")

    with patch("subprocess.run") as mock_run:
        with patch("syncit.bundle.archive.detect_bundle") as mock_detect:
            mock_detect.return_value.__enter__.return_value = tmp_path
            (tmp_path / "bundle.yaml").write_text(Path("tests/fixtures/bundle.yaml").read_text())
            result = runner.invoke(
                app, ["apply-remote", "--bundle", str(bundle_path), "--print-script"]
            )

    assert result.exit_code == 0
    assert "#!/usr/bin/env bash" in result.output
    assert "Extracting bundle archive..." in result.output

    # Should be no network interaction
    mock_run.assert_not_called()


def test_apply_remote_skips_unchanged(tmp_path: Path) -> None:
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
    # The bundle dir has no artifact subdirs, so checksums will be "sha256:" (empty)
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
                            "apply-remote",
                            "--bundle",
                            str(bundle_path),
                            "-i",
                            str(inv_file),
                            "-t",
                            "h1",
                        ],
                    )

    assert result.exit_code == 0

    # Popen should NEVER be called — all tasks skipped
    mock_popen.assert_not_called()

    # Output should indicate SKIP for each task
    assert "SKIP" in result.output

    # subprocess.run: SCP, extract, cat state.json = 3 calls (no tee pushes needed)
    assert mock_run.call_count == 3


def test_apply_remote_failed_task_aborts(tmp_path: Path) -> None:
    """If a task fails (non-zero exit code), state is updated with 'failed' and pipeline aborts."""
    inv_file = tmp_path / "inv.yaml"
    inv_file.write_text(
        "hosts:\n  h1:\n    host: 10.0.0.1\n    user: root\n    state_file: /s.json\n    bundle_dest: /opt"
    )

    bundle_path = tmp_path / "b.tar.gz"
    bundle_path.write_text("fake tar")

    # Use a simple manifest with only 2 tasks so we can control which one fails
    simple_manifest = (
        "apiVersion: syncit/v1\n"
        "kind: Bundle\n"
        "metadata:\n"
        "  name: test-fail\n"
        "  version: '1.0'\n"
        "spec:\n"
        "  targets:\n"
        "    distro: ubuntu\n"
        "    codename: noble\n"
        "    arch: amd64\n"
        "  tasks:\n"
        "    - name: Install packages\n"
        "      plugin: apt\n"
        "      packages:\n"
        "        - git\n"
        "    - name: Install Pip Deps\n"
        "      plugin: pip\n"
        "      python_version: '3.11'\n"
        "      requirements: ./requirements.txt\n"
    )

    # subprocess.run calls: SCP, extract, cat state.json, tee (after first task fails)
    run_side_effects = [
        MagicMock(returncode=0),  # SCP
        MagicMock(returncode=0),  # extract
        MagicMock(returncode=1, stdout=""),  # cat state.json -> not found
        MagicMock(returncode=0),  # tee after first task fails
    ]

    with patch("subprocess.run") as mock_run:
        mock_run.side_effect = run_side_effects
        with patch("subprocess.Popen") as mock_popen:
            # First Popen call (apt task) fails
            mock_proc_fail = MagicMock()
            mock_proc_fail.returncode = 1
            mock_proc_fail.stdin = MagicMock()
            mock_popen.return_value = mock_proc_fail

            with patch("shutil.which", return_value="ssh"):
                with patch("syncit.bundle.archive.detect_bundle") as mock_detect:
                    mock_detect.return_value.__enter__.return_value = tmp_path
                    (tmp_path / "bundle.yaml").write_text(simple_manifest)

                    result = runner.invoke(
                        app,
                        [
                            "apply-remote",
                            "--bundle",
                            str(bundle_path),
                            "-i",
                            str(inv_file),
                            "-t",
                            "h1",
                        ],
                    )

    # Should have exited with error
    assert result.exit_code != 0

    # Only 1 Popen call — first task failed, second was never reached
    assert mock_popen.call_count == 1

    # Output should indicate FAILED
    assert "FAILED" in result.output

    # Verify state was pushed with "failed" status via tee
    tee_call = mock_run.call_args_list[3]
    state_json_input = tee_call[1].get("input", tee_call[0][1] if len(tee_call[0]) > 1 else None)
    if state_json_input:
        import json

        state_data = json.loads(state_json_input)
        assert state_data["applied_tasks"]["Install packages"]["status"] == "failed"
