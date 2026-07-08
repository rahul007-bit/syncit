"""Tests for the pip plugin."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from syncit.plugins.base import ApplyContext, PackContext
from syncit.plugins.pip import PipPlugin


@pytest.fixture
def plugin() -> PipPlugin:
    return PipPlugin()


@pytest.fixture
def pack_ctx(tmp_bundle_dir: Path, fixture_dir: Path) -> PackContext:
    return PackContext(
        bundle_dir=tmp_bundle_dir,
        manifest_dir=fixture_dir,  # manifest_dir so relative paths resolve
        dry_run=False,
        task_slug="pip",
    )


@pytest.fixture
def apply_ctx(tmp_bundle_dir: Path, tmp_state_file: Path) -> ApplyContext:
    return ApplyContext(
        bundle_dir=tmp_bundle_dir,
        state_file=tmp_state_file,
        dry_run=False,
        task_slug="pip",
    )


# --------------------------------------------------------------------------- #
# validate()                                                                    #
# --------------------------------------------------------------------------- #


class TestPipValidate:
    def test_valid_requirements_spec(self, plugin: PipPlugin) -> None:
        assert plugin.validate({"requirements": "reqs.txt"}) == []

    def test_valid_pyproject_spec(self, plugin: PipPlugin) -> None:
        # pyproject is valid even though it's not fully implemented
        assert plugin.validate({"pyproject": "pyproject.toml"}) == []

    def test_missing_both_fields_returns_error(self, plugin: PipPlugin) -> None:
        errors = plugin.validate({})
        assert len(errors) >= 1
        assert "requirements" in errors[0] or "pyproject" in errors[0]

    def test_non_string_requirements_returns_error(self, plugin: PipPlugin) -> None:
        errors = plugin.validate({"requirements": 123})
        assert len(errors) >= 1


# --------------------------------------------------------------------------- #
# pack()                                                                        #
# --------------------------------------------------------------------------- #


class TestPipPack:
    def test_dry_run_no_subprocess(self, plugin: PipPlugin, pack_ctx: PackContext) -> None:
        pack_ctx.dry_run = True
        result = plugin.pack({"requirements": "requirements.txt"}, pack_ctx)
        assert result.success is True
        assert "dry-run" in result.message.lower()
        assert result.artifacts == []

    def test_missing_requirements_file_returns_failure(
        self, plugin: PipPlugin, pack_ctx: PackContext
    ) -> None:
        result = plugin.pack({"requirements": "nonexistent.txt"}, pack_ctx)
        assert result.success is False
        assert result.errors

    def test_pack_calls_pip_download(
        self, plugin: PipPlugin, pack_ctx: PackContext, requirements_txt: Path
    ) -> None:
        mock_ok = MagicMock(returncode=0, stdout="", stderr="")

        with patch("syncit.plugins.pip.subprocess.run", return_value=mock_ok):
            result = plugin.pack({"requirements": "requirements.txt"}, pack_ctx)

        assert result.success is True
        assert result.artifacts  # wheel_dir + requirements.txt copied

    def test_pack_copies_requirements_to_bundle(
        self, plugin: PipPlugin, pack_ctx: PackContext
    ) -> None:
        mock_ok = MagicMock(returncode=0, stdout="", stderr="")
        with patch("syncit.plugins.pip.subprocess.run", return_value=mock_ok):
            plugin.pack({"requirements": "requirements.txt"}, pack_ctx)

        assert (pack_ctx.bundle_dir / "pip" / "requirements.txt").exists()

    def test_pack_retries_without_only_binary_on_failure(
        self, plugin: PipPlugin, pack_ctx: PackContext
    ) -> None:
        fail = MagicMock(returncode=1, stdout="", stderr="no binary")
        ok = MagicMock(returncode=0, stdout="", stderr="")

        call_count = {"n": 0}

        def side_effect(cmd, **kwargs):
            call_count["n"] += 1
            if "--only-binary=:all:" in cmd:
                return fail
            return ok

        with patch("syncit.plugins.pip.subprocess.run", side_effect=side_effect):
            result = plugin.pack({"requirements": "requirements.txt"}, pack_ctx)

        assert result.success is True
        assert call_count["n"] == 2  # first attempt + retry

    def test_pyproject_not_supported_returns_failure(
        self, plugin: PipPlugin, pack_ctx: PackContext
    ) -> None:
        result = plugin.pack({"pyproject": "pyproject.toml"}, pack_ctx)
        assert result.success is False


# --------------------------------------------------------------------------- #
# apply()                                                                       #
# --------------------------------------------------------------------------- #


class TestPipApply:
    def _setup_bundle(self, bundle_dir: Path) -> None:
        wheels = bundle_dir / "pip" / "wheels"
        wheels.mkdir(parents=True)
        (bundle_dir / "pip" / "requirements.txt").write_text("requests==2.31.0\n")

    def test_dry_run_returns_success(self, plugin: PipPlugin, apply_ctx: ApplyContext) -> None:
        self._setup_bundle(apply_ctx.bundle_dir)
        apply_ctx.dry_run = True
        result = plugin.apply({"requirements": "requirements.txt"}, apply_ctx)
        assert result.success is True
        assert "dry-run" in result.message.lower()

    def test_missing_artifacts_returns_failure(
        self, plugin: PipPlugin, apply_ctx: ApplyContext
    ) -> None:
        result = plugin.apply({}, apply_ctx)
        assert result.success is False

    def test_apply_writes_pip_conf(self, plugin: PipPlugin, apply_ctx: ApplyContext) -> None:
        self._setup_bundle(apply_ctx.bundle_dir)
        mock_ok = MagicMock(returncode=0, stdout="[]", stderr="")

        with patch("syncit.plugins.pip.subprocess.run", return_value=mock_ok):
            with patch("syncit.plugins.pip.shutil.copytree"):
                with patch("syncit.plugins.pip.shutil.copy2"):
                    with patch("syncit.plugins.pip.Path.mkdir"):
                        with patch("syncit.plugins.pip.Path.write_text") as mock_write:
                            plugin.apply({}, apply_ctx)

        written_texts = [str(c.args[0]) for c in mock_write.call_args_list if c.args]
        assert any("no-index" in t for t in written_texts)

    def test_apply_runs_pip_install(self, plugin: PipPlugin, apply_ctx: ApplyContext) -> None:
        self._setup_bundle(apply_ctx.bundle_dir)
        mock_ok = MagicMock(returncode=0, stdout="[]", stderr="")

        with patch("syncit.plugins.pip.subprocess.run", return_value=mock_ok) as mock_run:
            with patch("syncit.plugins.pip.shutil.copytree"):
                with patch("syncit.plugins.pip.shutil.copy2"):
                    with patch("syncit.plugins.pip.Path.mkdir"):
                        with patch("syncit.plugins.pip.Path.write_text"):
                            plugin.apply({}, apply_ctx)

        install_calls = [
            c for c in mock_run.call_args_list if c.args and "install" in str(c.args[0])
        ]
        assert install_calls, "pip install should have been called"


# --------------------------------------------------------------------------- #
# diff()                                                                        #
# --------------------------------------------------------------------------- #


class TestPipDiff:
    def test_diff_new_task_marks_all_added(self, plugin: PipPlugin) -> None:
        result = plugin.diff(None, {"requirements": "reqs.txt"})
        assert result.added != []
        assert result.removed == []

    def test_diff_same_spec_unchanged(self, plugin: PipPlugin) -> None:
        spec = {"requirements": "reqs.txt", "python_version": "3.11"}
        result = plugin.diff(spec, spec)
        assert result.added == []
        assert result.removed == []
        assert result.unchanged

    def test_diff_changed_spec_marked_updated(self, plugin: PipPlugin) -> None:
        result = plugin.diff(
            {"requirements": "old_reqs.txt"},
            {"requirements": "new_reqs.txt"},
        )
        assert result.updated != []
