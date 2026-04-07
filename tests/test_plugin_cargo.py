from pathlib import Path
from unittest.mock import MagicMock, patch

from offlinectl.plugins.base import ApplyContext, PackContext
from offlinectl.plugins.cargo import CargoPlugin


def test_cargo_validate() -> None:
    plugin = CargoPlugin()

    with patch("shutil.which", return_value="cargo"):
        assert not plugin.validate({"projects": [{"project_name": "app", "project_dir": "/tmp"}]})

    with patch("shutil.which", return_value=None):
        assert (
            len(plugin.validate({"projects": [{"project_name": "app", "project_dir": "/tmp"}]}))
            == 1
        )


def test_cargo_pack(tmp_path: Path) -> None:
    plugin = CargoPlugin()
    bundle_dir = tmp_path / "bundle"
    ctx = PackContext(bundle_dir=bundle_dir, manifest_dir=tmp_path)

    proj_dir = tmp_path / "proj"
    proj_dir.mkdir()
    (proj_dir / "Cargo.toml").touch()

    spec = {"projects": [{"project_name": "app", "project_dir": str(proj_dir)}]}

    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout="[vendored]")
        res = plugin.pack(spec, ctx)

    assert res.success
    mock_run.assert_called_once()
    args = mock_run.call_args[0][0]
    assert args[0] == "cargo"
    assert args[1] == "vendor"

    target_dir = bundle_dir / "cargo" / "app"
    assert (target_dir / "config.toml.snippet").exists()
    assert (target_dir / "config.toml.snippet").read_text() == "[vendored]"


def test_cargo_apply(tmp_path: Path) -> None:
    plugin = CargoPlugin()
    bundle_dir = tmp_path / "bundle"
    target_dir = bundle_dir / "cargo" / "app"
    target_dir.mkdir(parents=True)

    vendor_src = target_dir / "vendor"
    vendor_src.mkdir()
    (vendor_src / "crate").touch()
    (target_dir / "config.toml.snippet").write_text("[snippet]")

    ctx = ApplyContext(bundle_dir=bundle_dir, state_file=tmp_path / "state.json")

    proj_dir = tmp_path / "proj"
    spec = {"projects": [{"project_name": "app", "project_dir": str(proj_dir)}]}

    res = plugin.apply(spec, ctx)
    assert res.success

    assert (proj_dir / "vendor" / "crate").exists()
    assert (proj_dir / ".cargo" / "config.toml").exists()
    assert "[snippet]" in (proj_dir / ".cargo" / "config.toml").read_text()
