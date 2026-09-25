"""Tests for the PyPI package browser (fetch + dep-closure resolution)."""

import io
import json
from unittest.mock import patch

from syncit.wizard import pypi_browser as pb

PYPI_JSON = {
    "info": {"name": "requests", "summary": "Python HTTP for Humans."},
    "releases": {"2.31.0": [], "2.10.0": [], "2.5.0": [], "2.32.3": []},
}


class _FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def _urlopen_ok(req, timeout=None, **kwargs):
    return _FakeResponse(json.dumps(PYPI_JSON).encode())


def test_fetch_package_sorts_versions_newest_first():
    with patch("syncit.wizard.pypi_browser.urllib.request.urlopen", _urlopen_ok):
        data = pb.fetch_package("requests")
    assert data is not None
    assert data["name"] == "requests"
    assert data["versions"][0] == "2.32.3"
    # numeric-aware ordering (2.31.0 must come before 2.10.0)
    assert data["versions"].index("2.31.0") < data["versions"].index("2.10.0")


def test_fetch_package_not_found():
    import urllib.error

    def _urlopen_404(req, timeout=None, **kwargs):
        raise urllib.error.HTTPError("https://pypi.org", 404, "Not Found", None, io.BytesIO(b""))

    with patch("syncit.wizard.pypi_browser.urllib.request.urlopen", _urlopen_404):
        assert pb.fetch_package("no-such-package-xyz-404") is None


def test_resolve_pip_deps_parses_report(tmp_path):
    report_data = {
        "install": [
            {"metadata": {"name": "requests", "version": "2.31.0"}, "requested": True},
            {"metadata": {"name": "charset-normalizer", "version": "3.3.2"}},
        ]
    }

    def _fake_run(cmd, capture_output=True, text=True, timeout=None):
        # The report file path is passed right after --report
        report_path = cmd[cmd.index("--report") + 1]
        with open(report_path, "w") as f:
            json.dump(report_data, f)
        from subprocess import CompletedProcess

        return CompletedProcess(cmd, 0, "", "")

    with patch("syncit.wizard.pypi_browser.subprocess.run", _fake_run):
        deps = pb.resolve_pip_deps("requests==2.31.0")

    assert {"name": "requests", "version": "2.31.0"} in deps
    assert {"name": "charset-normalizer", "version": "3.3.2"} in deps


def test_resolve_pip_deps_retries_without_python_version(tmp_path):
    """--python-version attempt fails -> falls back to plain resolution."""
    calls = []

    def _fake_run(cmd, capture_output=True, text=True, timeout=None):
        calls.append(list(cmd))
        from subprocess import CompletedProcess

        if "--python-version" in cmd:
            return CompletedProcess(cmd, 1, "", "error: --python-version requires ...")
        report_path = cmd[cmd.index("--report") + 1]
        with open(report_path, "w") as f:
            json.dump({"install": [{"metadata": {"name": "flask", "version": "3.0.0"}}]}, f)
        return CompletedProcess(cmd, 0, "", "")

    with patch("syncit.wizard.pypi_browser.subprocess.run", _fake_run):
        deps = pb.resolve_pip_deps("flask", python_version="3.12")

    assert len(calls) == 2
    assert "--python-version" in calls[0]
    assert "--python-version" not in calls[1]
    assert deps == [{"name": "flask", "version": "3.0.0"}]
