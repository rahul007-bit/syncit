from pathlib import Path
from unittest.mock import MagicMock, patch

from syncit.plugins.base import ApplyContext, PackContext
from syncit.plugins.dnf import DnfPlugin


def test_dnf_validate() -> None:
    plugin = DnfPlugin()

    with patch("shutil.which", side_effect=lambda x: x in ["dnf", "createrepo_c"]):
        assert not plugin.validate({"packages": ["nginx", "postgis"]})

    with patch("shutil.which", return_value=None):
        res = plugin.validate({"packages": ["nginx"]})
        assert len(res) == 2  # missing both dnf and createrepo_c


@patch("subprocess.run")
def test_dnf_pack(mock_run, tmp_path: Path) -> None:
    mock_run.return_value = MagicMock(returncode=0)

    plugin = DnfPlugin()
    bundle_dir = tmp_path / "bundle"
    ctx = PackContext(bundle_dir=bundle_dir, manifest_dir=tmp_path)

    res = plugin.pack({"packages": ["nginx"]}, ctx)
    assert res.success

    assert mock_run.call_count == 2
    args1 = mock_run.call_args_list[0][0][0]
    assert args1[0:2] == ["dnf", "download"]
    assert "nginx" in args1

    args2 = mock_run.call_args_list[1][0][0]
    assert args2[0] == "createrepo_c"

    assert (bundle_dir / "dnf" / "rpms").exists()


@patch("subprocess.run")
def test_dnf_apply(mock_run, tmp_path: Path) -> None:
    mock_run.return_value = MagicMock(returncode=0)

    plugin = DnfPlugin()
    bundle_dir = tmp_path / "bundle"
    rpm_bundled = bundle_dir / "dnf" / "rpms"
    rpm_bundled.mkdir(parents=True)
    (rpm_bundled / "nginx.rpm").touch()

    ctx = ApplyContext(bundle_dir=bundle_dir, state_file=tmp_path / "state.json")

    sys_rpm = tmp_path / "srv"
    sys_repo = tmp_path / "offline.repo"

    original_path = Path

    class MockPath(original_path):
        def __new__(cls, *args, **kwargs):
            if str(args[0]) == "/srv/offline/dnf/rpms":
                return original_path(str(sys_rpm))
            if str(args[0]) == "/etc/yum.repos.d/offline.repo":
                return original_path(str(sys_repo))
            return original_path(*args, **kwargs)

    with patch("syncit.plugins.dnf.Path", new=MockPath):
        res = plugin.apply({"packages": ["nginx"]}, ctx)

    assert res.success
    assert mock_run.call_count == 2

    assert sys_rpm.exists()
    assert (sys_rpm / "nginx.rpm").exists()

    assert sys_repo.exists()
    assert "pgcheck=0" in sys_repo.read_text() or "gpgcheck=0" in sys_repo.read_text()
