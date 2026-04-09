import pytest
from typer.testing import CliRunner
from syncit.main import app
from pathlib import Path
from unittest.mock import patch, MagicMock

runner = CliRunner()


def f():
    tmp_path = Path("/tmp/tst")
    tmp_path.mkdir(exist_ok=True)
    inv_file = tmp_path / "inv.yaml"
    inv_file.write_text(
        "hosts:\n  h1:\n    host: 10.0.0.1\n    user: root\n    state_file: /s.json\n    bundle_dest: /opt"
    )
    bundle_path = tmp_path / "b.tar.gz"
    bundle_path.write_text("fake tar")
    with patch("subprocess.run") as mock_run:
        mock_run.return_value.returncode = 0
        with patch("subprocess.Popen") as mock_popen:
            mock_proc = MagicMock()
            mock_proc.returncode = 0
            mock_proc.stdout = []
            mock_popen.return_value.__enter__.return_value = mock_proc
            with patch("shutil.which", return_value="ssh"):
                with patch("syncit.bundle.archive.detect_bundle") as mock_detect:
                    mock_detect.return_value.__enter__.return_value = tmp_path
                    (tmp_path / "bundle.yaml").write_text(
                        "apiVersion: syncit/v1\nkind: Bundle\nmetadata:\n  name: b\n  version: 1.0\nspec:\n  tasks: []"
                    )
                    result = runner.invoke(
                        app, ["apply-remote", str(bundle_path), "-i", str(inv_file), "-t", "h1"]
                    )
                    print(result.output)
                    if result.exception:
                        print(result.exception)


f()
