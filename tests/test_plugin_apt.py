"""Tests for the apt plugin."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from offlinectl.plugins.apt import AptPlugin
from offlinectl.plugins.base import ApplyContext, PackContext


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
        assert plugin.validate({"packages": ["git", "curl"]}) == []

    def test_missing_packages_returns_error(self, plugin: AptPlugin) -> None:
        errors = plugin.validate({})
        assert len(errors) == 1
        assert "packages" in errors[0]

    def test_empty_packages_list_returns_error(self, plugin: AptPlugin) -> None:
        errors = plugin.validate({"packages": []})
        assert len(errors) >= 1

    def test_packages_not_list_returns_error(self, plugin: AptPlugin) -> None:
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
        mock_result.stdout = "Package: git\n"
        mock_result.stderr = ""

        with patch("offlinectl.plugins.apt.subprocess.run", return_value=mock_result) as mock_run:
            plugin.pack({"packages": ["git"]}, pack_ctx)

        # apt-cache depends should be called
        calls = [c.args[0] for c in mock_run.call_args_list]
        assert any("apt-cache" in str(c) for c in calls)
        # apt-get download should be called
        assert any("apt-get" in str(c) for c in calls)
        # dpkg-scanpackages should be called
        assert any("dpkg-scanpackages" in str(c) for c in calls)

    def test_pack_creates_packages_file(self, plugin: AptPlugin, pack_ctx: PackContext) -> None:
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "Package: git\n"
        mock_result.stderr = ""

        with patch("offlinectl.plugins.apt.subprocess.run", return_value=mock_result):
            plugin.pack({"packages": ["git"]}, pack_ctx)

        assert (pack_ctx.bundle_dir / "apt" / "Packages").exists()

    def test_pack_creates_sources_list(self, plugin: AptPlugin, pack_ctx: PackContext) -> None:
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = ""
        mock_result.stderr = ""

        with patch("offlinectl.plugins.apt.subprocess.run", return_value=mock_result):
            plugin.pack({"packages": ["git"]}, pack_ctx)

        sources = (pack_ctx.bundle_dir / "apt" / "sources.list").read_text()
        assert "file://" in sources
        assert "trusted=yes" in sources


# --------------------------------------------------------------------------- #
# apply()                                                                       #
# --------------------------------------------------------------------------- #


class TestAptApply:
    def _setup_bundle(self, bundle_dir: Path) -> None:
        """Create minimal apt bundle artifacts."""
        apt_dir = bundle_dir / "apt"
        debs_dir = apt_dir / "debs"
        debs_dir.mkdir(parents=True)
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

        with patch("offlinectl.plugins.apt.subprocess.run", side_effect=side_effect):
            with patch("offlinectl.plugins.apt.shutil.copytree"):
                with patch("offlinectl.plugins.apt.Path.mkdir"):
                    with patch("offlinectl.plugins.apt.Path.write_text"):
                        result = plugin.apply({"packages": ["git"]}, apply_ctx)

        assert result.success is True

    def test_idempotence_skips_installed_packages(
        self, plugin: AptPlugin, apply_ctx: ApplyContext
    ) -> None:
        self._setup_bundle(apply_ctx.bundle_dir)

        mock_ok = MagicMock(returncode=0, stdout="ii  git", stderr="")

        with patch("offlinectl.plugins.apt.subprocess.run", return_value=mock_ok):
            with patch("offlinectl.plugins.apt.shutil.copytree"):
                with patch("offlinectl.plugins.apt.Path.mkdir"):
                    with patch("offlinectl.plugins.apt.Path.write_text"):
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
