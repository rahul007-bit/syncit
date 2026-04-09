"""Tests for the exec command."""

from __future__ import annotations

from concurrent.futures import Future
from pathlib import Path
from unittest.mock import MagicMock, patch

from typer.testing import CliRunner

from syncit.main import app

runner = CliRunner()


def _make_inv(
    hosts: dict[str, dict] | None = None,
    groups: dict[str, list[str]] | None = None,
) -> Path:
    """Write a minimal inventory YAML and return the path."""
    import yaml

    path = Path("/tmp/inv.yaml")
    data: dict = {}
    if hosts:
        data["hosts"] = hosts
    if groups:
        data["groups"] = groups
    path.write_text(yaml.dump(data))
    return path


class TestExecSingleTarget:
    def test_runs_correct_ssh_command(self, tmp_path: Path) -> None:
        inv_file = tmp_path / "inv.yaml"
        inv_file.write_text("hosts:\n  airgapped-test:\n    host: 10.0.0.1\n    user: root\n")

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="disk usage\n", stderr="")
            result = runner.invoke(
                app,
                ["exec", "-i", str(inv_file), "-t", "airgapped-test", "--", "df", "-h"],
            )

        assert result.exit_code == 0
        ssh_call = mock_run.call_args[0][0]
        assert ssh_call[0] == "ssh"
        assert ssh_call[1] == "root@10.0.0.1"
        assert ssh_call[-3:] == ["bash", "-c", "df -h"]

    def test_sudo_prepends_sudo_bash_c(self, tmp_path: Path) -> None:
        inv_file = tmp_path / "inv.yaml"
        inv_file.write_text("hosts:\n  airgapped-test:\n    host: 10.0.0.1\n    user: root\n")

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            result = runner.invoke(
                app,
                [
                    "exec",
                    "-i",
                    str(inv_file),
                    "-t",
                    "airgapped-test",
                    "--sudo",
                    "--",
                    "systemctl",
                    "status",
                    "docker",
                ],
            )

        assert result.exit_code == 0
        ssh_call = mock_run.call_args[0][0]
        assert ssh_call[-4:] == ["sudo", "bash", "-c", "systemctl status docker"]

    def test_ssh_key_is_used(self, tmp_path: Path) -> None:
        inv_file = tmp_path / "inv.yaml"
        inv_file.write_text(
            "hosts:\n  h1:\n    host: 10.0.0.1\n    user: root\n    ssh_key: /tmp/id_rsa\n"
        )

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            result = runner.invoke(
                app,
                ["exec", "-i", str(inv_file), "-t", "h1", "--", "uptime"],
            )

        assert result.exit_code == 0
        ssh_call = mock_run.call_args[0][0]
        assert "-i" in ssh_call
        assert "/tmp/id_rsa" in ssh_call


class TestExecGroup:
    def test_group_resolves_hosts(self, tmp_path: Path) -> None:
        inv_file = tmp_path / "inv.yaml"
        inv_file.write_text(
            "hosts:\n  h1:\n    host: 10.0.0.1\n    user: root\n  h2:\n    host: 10.0.0.2\n    user: root\n"
            "groups:\n  all-offline:\n    - h1\n    - h2\n"
        )

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            result = runner.invoke(
                app,
                ["exec", "-i", str(inv_file), "-g", "all-offline", "--", "uptime"],
            )

        assert result.exit_code == 0
        assert mock_run.call_count == 2


class TestExecAllHosts:
    def test_all_resolves_all_hosts(self, tmp_path: Path) -> None:
        inv_file = tmp_path / "inv.yaml"
        inv_file.write_text(
            "hosts:\n  h1:\n    host: 10.0.0.1\n    user: root\n  h2:\n    host: 10.0.0.2\n    user: root\n"
        )

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            result = runner.invoke(
                app,
                ["exec", "-i", str(inv_file), "--all", "--", "uptime"],
            )

        assert result.exit_code == 0
        assert mock_run.call_count == 2


class TestExecMutex:
    def test_error_when_no_target_flag_provided(self, tmp_path: Path) -> None:
        inv_file = tmp_path / "inv.yaml"
        inv_file.write_text("hosts:\n  h1:\n    host: 10.0.0.1\n    user: root\n")

        result = runner.invoke(
            app,
            ["exec", "-i", str(inv_file), "--", "uptime"],
        )

        assert result.exit_code == 1
        assert "Must provide" in result.output


class TestExecParallel:
    def test_uses_thread_pool_executor(self, tmp_path: Path) -> None:
        inv_file = tmp_path / "inv.yaml"
        inv_file.write_text(
            "hosts:\n  h1:\n    host: 10.0.0.1\n    user: root\n  h2:\n    host: 10.0.0.2\n    user: root\n"
        )

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            with patch("syncit.commands.exec_cmd.ThreadPoolExecutor") as mock_tpe:
                mock_executor = MagicMock()
                mock_tpe.return_value.__enter__ = MagicMock(return_value=mock_executor)
                mock_tpe.return_value.__exit__ = MagicMock()
                # Make submit return an already-done Future so as_completed yields immediately
                done_future: Future = Future()  # type: ignore[abstract]
                done_future.set_result(None)
                mock_executor.submit.return_value = done_future

                with patch("syncit.commands.exec_cmd.as_completed") as mock_as_completed:
                    # Return an iterable over the submitted futures
                    mock_as_completed.return_value = iter(mock_executor.submit.call_args_list)

                    result = runner.invoke(
                        app,
                        ["exec", "-i", str(inv_file), "--all", "--", "uptime"],
                    )

        assert result.exit_code == 0
        mock_tpe.assert_called_once_with(max_workers=2)
        assert mock_executor.submit.call_count == 2


class TestExecFailure:
    def test_exit_code_1_when_host_fails(self, tmp_path: Path) -> None:
        inv_file = tmp_path / "inv.yaml"
        inv_file.write_text(
            "hosts:\n  h1:\n    host: 10.0.0.1\n    user: root\n  h2:\n    host: 10.0.0.2\n    user: root\n"
        )

        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = [
                MagicMock(returncode=0, stdout="h1 ok\n", stderr=""),
                MagicMock(returncode=1, stdout="", stderr="h2 error\n"),
            ]
            result = runner.invoke(
                app,
                ["exec", "-i", str(inv_file), "--all", "--", "echo ok"],
            )

        assert result.exit_code == 1
        assert "✗" in result.output
        assert "[h1]" in result.output
        assert "[h2]" in result.output

    def test_exit_code_0_when_all_succeed(self, tmp_path: Path) -> None:
        inv_file = tmp_path / "inv.yaml"
        inv_file.write_text(
            "hosts:\n  h1:\n    host: 10.0.0.1\n    user: root\n  h2:\n    host: 10.0.0.2\n    user: root\n"
        )

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="ok\n", stderr="")
            result = runner.invoke(
                app,
                ["exec", "-i", str(inv_file), "--all", "--", "echo ok"],
            )

        assert result.exit_code == 0
        assert "✓" in result.output
        assert "[h1]" in result.output
        assert "[h2]" in result.output
