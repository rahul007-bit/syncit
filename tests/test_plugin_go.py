from pathlib import Path
from unittest.mock import MagicMock, patch

from syncit.plugins.base import ApplyContext, PackContext
from syncit.plugins.go import GoPlugin


def test_go_validate() -> None:
    plugin = GoPlugin()

    with patch("shutil.which", return_value="go"):
        assert not plugin.validate({"projects": [{"project_name": "app", "project_dir": "/tmp"}]})

    with patch("shutil.which", return_value=None):
        assert (
            len(plugin.validate({"projects": [{"project_name": "app", "project_dir": "/tmp"}]}))
            == 1
        )


def test_go_pack(tmp_path: Path) -> None:
    plugin = GoPlugin()
    bundle_dir = tmp_path / "bundle"
    ctx = PackContext(bundle_dir=bundle_dir, manifest_dir=tmp_path)

    proj_dir = tmp_path / "proj"
    proj_dir.mkdir()
    (proj_dir / "go.mod").touch()

    spec = {"projects": [{"project_name": "app", "project_dir": str(proj_dir)}]}

    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0)
        res = plugin.pack(spec, ctx)

    assert res.success
    assert mock_run.call_count == 2
    # Check the second call (seeding bundle)
    call_args, call_kwargs = mock_run.call_args_list[1]
    assert call_args[0] == ["go", "mod", "download", "./..."]
    assert "GOMODCACHE" in call_kwargs["env"]
    assert str(call_kwargs["env"]["GOMODCACHE"]).endswith("go/modcache")


def test_go_apply(tmp_path: Path, monkeypatch) -> None:
    plugin = GoPlugin()
    bundle_dir = tmp_path / "bundle"

    modcache_src = bundle_dir / "go" / "modcache"
    modcache_src.mkdir(parents=True)
    (modcache_src / "download").touch()

    ctx = ApplyContext(bundle_dir=bundle_dir, state_file=tmp_path / "state.json")
    spec = {"projects": [{"project_name": "app", "project_dir": str(tmp_path)}]}

    # Mock system paths for testing locally without root
    sys_opt = tmp_path / "opt" / "offline" / "go" / "modcache"
    sys_profile = tmp_path / "etc" / "profile.d" / "offline-go.sh"

    # We patch pathlib.Path so instances pointed to the system paths get rerouted
    original_path = Path

    class MockPath(original_path):
        def __new__(cls, *args, **kwargs):
            if str(args[0]) == "/opt/offline/go/modcache":
                return original_path(str(sys_opt))
            if str(args[0]) == "/etc/profile.d/offline-go.sh":
                return original_path(str(sys_profile))
            return original_path(*args, **kwargs)

    with patch("syncit.plugins.go.Path", new=MockPath):
        res = plugin.apply(spec, ctx)

    assert res.success
    assert sys_opt.exists()
    assert (sys_opt / "download").exists()
    assert sys_profile.exists()
    text = sys_profile.read_text()
    assert "GOMODCACHE=" in text
    assert "GOPROXY=off" in text
