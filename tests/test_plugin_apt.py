"""Tests for the apt plugin."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from syncit.plugins.apt import AptPlugin
from syncit.plugins.base import ApplyContext, PackContext


@pytest.fixture
def plugin() -> AptPlugin:
    return AptPlugin()


@pytest.fixture
def pack_ctx(tmp_bundle_dir: Path) -> PackContext:
    return PackContext(bundle_dir=tmp_bundle_dir, manifest_dir=tmp_bundle_dir, dry_run=False)


@pytest.fixture
def apply_ctx(tmp_bundle_dir: Path, tmp_state_file: Path) -> ApplyContext:
    return ApplyContext(bundle_dir=tmp_bundle_dir, state_file=tmp_state_file, dry_run=False)


# --------------------------------------------------------------------------- #
# validate()                                                                    #
# --------------------------------------------------------------------------- #


class TestAptValidate:
    def test_valid_spec_returns_no_errors(self, plugin: AptPlugin) -> None:
        with patch("shutil.which", return_value="path"):
            assert plugin.validate({"packages": ["git", "curl"]}) == []

    def test_missing_dpkg_scanpackages(self, plugin: AptPlugin) -> None:
        with patch("shutil.which", return_value=None):
            errors = plugin.validate({"packages": ["git"]})
            assert len(errors) == 1
            assert "dpkg-scanpackages" in errors[0]

    def test_missing_packages_returns_error(self, plugin: AptPlugin) -> None:
        with patch("shutil.which", return_value="path"):
            errors = plugin.validate({})
            assert len(errors) == 1
            assert "packages" in errors[0]

    def test_empty_packages_list_returns_error(self, plugin: AptPlugin) -> None:
        with patch("shutil.which", return_value="path"):
            errors = plugin.validate({"packages": []})
            assert len(errors) >= 1

    def test_packages_not_list_returns_error(self, plugin: AptPlugin) -> None:
        with patch("shutil.which", return_value="path"):
            errors = plugin.validate({"packages": "git"})
            assert len(errors) >= 1


# --------------------------------------------------------------------------- #
# pack()                                                                        #
# --------------------------------------------------------------------------- #


class TestAptPack:
    def test_dry_run_returns_success_no_subprocesses(
        self, plugin: AptPlugin, pack_ctx: PackContext
    ) -> None:
        pack_ctx.dry_run = True
        result = plugin.pack({"packages": ["git"]}, pack_ctx)
        assert result.success is True
        assert "dry-run" in result.message.lower()
        assert result.artifacts == []

    def test_pack_invokes_correct_commands(self, plugin: AptPlugin, pack_ctx: PackContext) -> None:
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "'http://archive.ubuntu.com/git.deb' git_2.43.0-1_amd64.deb 100 MD5Sum:123\n"
        mock_result.stderr = ""
        pack_ctx.no_cache = True

        with patch("syncit.plugins.apt.subprocess.run", return_value=mock_result) as mock_run:
            plugin.pack({"packages": ["git"]}, pack_ctx)

        calls = [c.args[0] for c in mock_run.call_args_list]
        # apt-get install --print-uris should be called
        assert any("install" in str(c) and "--print-uris" in str(c) for c in calls)
        # apt-get download should be called
        assert any("download" in str(c) for c in calls)
        # dpkg-scanpackages should be called
        assert any("dpkg-scanpackages" in str(c) for c in calls)

    def test_pack_creates_packages_file(self, plugin: AptPlugin, pack_ctx: PackContext) -> None:
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "'http://archive.ubuntu.com/git.deb' git_2.43.0-1_amd64.deb 100 MD5Sum:123\n"
        mock_result.stderr = ""

        with patch("syncit.plugins.apt.subprocess.run", return_value=mock_result):
            plugin.pack({"packages": ["git"]}, pack_ctx)

        assert (pack_ctx.bundle_dir / "default" / "Packages").exists()

    def test_pack_creates_sources_list(self, plugin: AptPlugin, pack_ctx: PackContext) -> None:
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "'http://archive.ubuntu.com/git.deb' git_2.43.0-1_amd64.deb 100 MD5Sum:123\n"
        mock_result.stderr = ""

        with patch("syncit.plugins.apt.subprocess.run", return_value=mock_result):
            plugin.pack({"packages": ["git"]}, pack_ctx)

        sources = (pack_ctx.bundle_dir / "default" / "sources.list").read_text()
        assert "file://" in sources
        assert "trusted=yes" in sources

    def test_pack_uses_base_installroot(self, plugin: AptPlugin, pack_ctx: PackContext, tmp_path: Path) -> None:
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "'http://archive.ubuntu.com/git.deb' git_2.43.0-1_amd64.deb 100 MD5Sum:123\n"
        mock_result.stderr = ""

        installroot = tmp_path / "root"
        status_file = installroot / "var" / "lib" / "dpkg" / "status"
        status_file.parent.mkdir(parents=True)
        status_file.touch()

        with patch("syncit.plugins.apt.subprocess.run", return_value=mock_result) as mock_run:
            plugin.pack({"packages": ["git"], "base_installroot": str(installroot)}, pack_ctx)

        calls = [c.args[0] for c in mock_run.call_args_list]
        install_call = [c for c in calls if "install" in str(c) and "--print-uris" in str(c)][0]
        assert f"Dir::State::status={status_file}" in str(install_call)


# --------------------------------------------------------------------------- #
# apply()                                                                       #
# --------------------------------------------------------------------------- #


class TestAptApply:
    def _setup_bundle(self, bundle_dir: Path) -> None:
        """Create minimal apt bundle artifacts."""
        apt_dir = bundle_dir / "default"
        apt_dir.mkdir(parents=True)
        (apt_dir / "Packages").write_text("Package: git\nVersion: 1:2.43\n")

    def test_dry_run_returns_success(self, plugin: AptPlugin, apply_ctx: ApplyContext) -> None:
        self._setup_bundle(apply_ctx.bundle_dir)
        apply_ctx.dry_run = True
        result = plugin.apply({"packages": ["git"]}, apply_ctx)
        assert result.success is True
        assert "dry-run" in result.message.lower()

    def test_missing_artifacts_returns_failure(
        self, plugin: AptPlugin, apply_ctx: ApplyContext
    ) -> None:
        result = plugin.apply({"packages": ["git"]}, apply_ctx)
        assert result.success is False
        assert result.errors

    def test_apply_calls_apt_get_install(self, plugin: AptPlugin, apply_ctx: ApplyContext) -> None:
        self._setup_bundle(apply_ctx.bundle_dir)

        mock_ok = MagicMock(returncode=0, stdout="", stderr="")
        mock_not_installed = MagicMock(returncode=1, stdout="", stderr="not found")

        def side_effect(cmd, **kwargs):
            if cmd[0] == "dpkg":
                return mock_not_installed
            return mock_ok

        with patch("syncit.plugins.apt.subprocess.run", side_effect=side_effect):
            with patch("syncit.plugins.apt.shutil.copytree"):
                with patch("syncit.plugins.apt.Path.mkdir"):
                    with patch("syncit.plugins.apt.Path.write_text"):
                        result = plugin.apply({"packages": ["git"]}, apply_ctx)

        assert result.success is True

    def test_idempotence_skips_installed_packages(
        self, plugin: AptPlugin, apply_ctx: ApplyContext
    ) -> None:
        self._setup_bundle(apply_ctx.bundle_dir)

        mock_ok = MagicMock(returncode=0, stdout="ii  git", stderr="")

        with patch("syncit.plugins.apt.subprocess.run", return_value=mock_ok):
            with patch("syncit.plugins.apt.shutil.copytree"):
                with patch("syncit.plugins.apt.Path.mkdir"):
                    with patch("syncit.plugins.apt.Path.write_text"):
                        result = plugin.apply({"packages": ["git"]}, apply_ctx)

        assert result.success is True
        assert (
            "nothing to do" in result.message.lower()
            or "already installed" in result.message.lower()
        )


# --------------------------------------------------------------------------- #
# diff()                                                                        #
# --------------------------------------------------------------------------- #


class TestAptDiff:
    def test_diff_new_task_marks_all_added(self, plugin: AptPlugin) -> None:
        result = plugin.diff(None, {"packages": ["git", "curl"]})
        assert set(result.added) == {"git", "curl"}
        assert result.removed == []
        assert result.updated == []

    def test_diff_added_packages(self, plugin: AptPlugin) -> None:
        result = plugin.diff(
            {"packages": ["git"]},
            {"packages": ["git", "curl"]},
        )
        assert "curl" in result.added
        assert "git" in result.unchanged

    def test_diff_removed_packages(self, plugin: AptPlugin) -> None:
        result = plugin.diff(
            {"packages": ["git", "curl"]},
            {"packages": ["git"]},
        )
        assert "curl" in result.removed
        assert "git" in result.unchanged

    def test_diff_no_changes(self, plugin: AptPlugin) -> None:
        spec = {"packages": ["git", "curl"]}
        result = plugin.diff(spec, spec)
        assert result.added == []
        assert result.removed == []
        assert set(result.unchanged) == {"git", "curl"}


# --------------------------------------------------------------------------- #
# render_apply_sh()                                                             #
# --------------------------------------------------------------------------- #


class TestAptRenderApplySh:
    def test_render_apply_sh_contains_isolation_flags(self, plugin: AptPlugin) -> None:
        snippet = plugin.render_apply_sh({"packages": ["curl", "git"]}, "apt")

        # Verify update command has isolation
        assert "apt-get update" in snippet
        assert (
            "-o Dir::Etc::SourceList="
            in snippet.split("apt-get update")[1].split("apt-get install")[0]
        )
        assert '-o Dir::Etc::SourceParts="/dev/null"' in snippet

        # Verify install command has isolation
        assert "apt-get install" in snippet
        assert "-o Dir::Etc::SourceList=" in snippet.split("apt-get install")[1]

        # Verify no system paths used for sources
        assert "/etc/apt/sources.list.d/" not in snippet
        assert 'SOURCES_FILE="$BUNDLE_DIR/apt/syncit.list"' in snippet


class TestAptEdgeCases:
    @patch("syncit.plugins.apt.urllib.request.urlretrieve")
    @patch("syncit.plugins.apt.subprocess.run")
    def test_pack_with_repos(self, mock_run, mock_urlretrieve, plugin: AptPlugin, pack_ctx: PackContext) -> None:
        # Mock apt-get install --print-uris output
        mock_result = MagicMock(returncode=0)
        mock_result.stdout = "'http://archive.ubuntu.com/git.deb' git_2.43.0-1_amd64.deb 100 MD5Sum:123\n"
        mock_result.stderr = ""

        # Whenever download is called, simulate writing downloaded file to cwd
        def side_effect(cmd, **kwargs):
            if "install" in cmd:
                return mock_result
            if "download" in cmd:
                cwd = kwargs.get("cwd")
                if cwd:
                    Path(cwd).joinpath("git_2.43.0-1_amd64.deb").write_text("dummy")
                return MagicMock(returncode=0)
            return MagicMock(returncode=0, stdout="Package: git\nVersion: 2.43.0-1\n")

        mock_run.side_effect = side_effect
        mock_urlretrieve.return_value = None

        spec = {
            "packages": ["git"],
            "repos": [{"name": "test-repo", "url": "deb http://example.com/repo noble main", "gpg_key": "https://example.com/key.gpg"}]
        }
        
        # Patch Path.read_bytes to return armor block and mock os-release
        with patch("syncit.plugins.apt.Path.read_bytes", return_value=b"-----BEGIN PGP PUBLIC KEY BLOCK-----"):
            with patch("syncit.plugins.apt.Path.rename"):
                res = plugin.pack(spec, pack_ctx)
        
        assert res.success
        assert mock_urlretrieve.called

    def test_pack_invalid_installroot(self, plugin: AptPlugin, pack_ctx: PackContext, tmp_path: Path) -> None:
        # 1. Nonexistent directory
        res = plugin.pack({"packages": ["git"], "base_installroot": str(tmp_path / "nonexistent")}, pack_ctx)
        assert not res.success
        assert "does not exist or is not a directory" in res.errors[0]

        # 2. Missing status file
        installroot = tmp_path / "ir"
        installroot.mkdir()
        res = plugin.pack({"packages": ["git"], "base_installroot": str(installroot)}, pack_ctx)
        assert not res.success
        assert "missing status file" in res.errors[0]

    @patch("syncit.plugins.apt.subprocess.run")
    def test_pack_dependency_resolution_fails(self, mock_run, plugin: AptPlugin, pack_ctx: PackContext) -> None:
        mock_run.return_value = MagicMock(returncode=1, stderr="unable to locate package")
        res = plugin.pack({"packages": ["git"]}, pack_ctx)
        assert not res.success
        assert "failed to resolve packages" in res.errors[0]
