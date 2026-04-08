from pathlib import Path
from unittest.mock import MagicMock, patch

from syncit.plugins.base import ApplyContext, PackContext
from syncit.plugins.npm import NpmPlugin


def test_npm_validate() -> None:
    plugin = NpmPlugin()

    with patch("shutil.which", return_value="npm"):
        # Valid format
        assert not plugin.validate({"projects": [{"project_name": "app", "project_dir": "/tmp"}]})
        # Missing keys
        assert len(plugin.validate({"projects": [{"project_name": "app"}]})) == 1
        assert len(plugin.validate({"projects": [{"project_dir": "/tmp"}]})) == 1

    with patch("shutil.which", return_value=None):
        assert (
            len(plugin.validate({"projects": [{"project_name": "app", "project_dir": "/tmp"}]}))
            == 1
        )


def test_npm_pack(tmp_path: Path) -> None:
    plugin = NpmPlugin()
    bundle_dir = tmp_path / "bundle"
    ctx = PackContext(bundle_dir=bundle_dir, manifest_dir=tmp_path)

    proj_dir = tmp_path / "proj"
    proj_dir.mkdir()
    (proj_dir / "package.json").touch()
    (proj_dir / "node_modules").mkdir()
    (proj_dir / "node_modules" / "some_pkg").touch()

    spec = {"projects": [{"project_name": "app", "project_dir": str(proj_dir)}]}

    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0)
        res = plugin.pack(spec, ctx)

    assert res.success
    mock_run.assert_called_once_with(
        ["npm", "ci"], cwd=proj_dir.resolve(), capture_output=True, text=True
    )

    target_nm = bundle_dir / "npm" / "app" / "node_modules"
    assert target_nm.exists()
    assert (target_nm / "some_pkg").exists()


def test_npm_apply(tmp_path: Path) -> None:
    plugin = NpmPlugin()
    bundle_dir = tmp_path / "bundle"
    bundled_nm = bundle_dir / "npm" / "app" / "node_modules"
    bundled_nm.mkdir(parents=True)
    (bundled_nm / "fake").touch()

    ctx = ApplyContext(bundle_dir=bundle_dir, state_file=tmp_path / "state.json")

    proj_dir = tmp_path / "proj"
    spec = {"projects": [{"project_name": "app", "project_dir": str(proj_dir)}]}

    res = plugin.apply(spec, ctx)
    assert res.success

    assert (proj_dir / "node_modules" / "fake").exists()

    npmrc = proj_dir / ".npmrc"
    assert npmrc.exists()
    text = npmrc.read_text()
    assert "offline=true" in text
    assert "prefer-offline=true" in text


def test_npm_diff() -> None:
    plugin = NpmPlugin()
    old = {"projects": [{"project_name": "p1", "project_dir": "d1"}]}
    new = {
        "projects": [
            {"project_name": "p1", "project_dir": "d1"},
            {"project_name": "p2", "project_dir": "d2"},
        ]
    }

    res = plugin.diff(old, new)
    assert bool(res.added or res.removed)
    assert len(res.added) == 1
    assert len(res.removed) == 0
