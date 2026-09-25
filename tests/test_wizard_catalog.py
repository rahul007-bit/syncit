"""Tests for the wizard repo catalog and pip requirements materialization."""

import io

from syncit.wizard import catalog as rc
from syncit.wizard import rpm_browser as rb
from syncit.commands.create import (
    _materialize_pip_requirements,
    _dnf_root_populated,
    _apt_root_populated,
)


# ── Repo catalog loading ─────────────────────────────────────────────────


def test_load_repos_rocky_returns_curated_entries():
    repos = rc.load_repos("rocky")
    ids = {r["id"] for r in repos}
    assert "epel" in ids
    assert "docker-ce" in ids
    assert "kubernetes" in ids


def test_load_repos_excludes_unsupported():
    # hashicorp has no Fedora repo in the catalog
    fedora_ids = {r["id"] for r in rc.load_repos("fedora")}
    assert "hashicorp" not in fedora_ids


def test_load_repos_unknown_distro_returns_empty():
    assert rc.load_repos("ubuntu") == []
    assert rc.load_repos("") == []


def test_render_repo_substitutes_releasever_and_basearch():
    entry = next(r for r in rc.load_repos("rocky") if r["id"] == "epel")
    out = rc.render_repo(entry, "rocky", releasever="9", basearch="x86_64")
    assert out["baseurl"] == "https://dl.fedoraproject.org/pub/epel/9/Everything/x86_64/"
    assert out["gpgkey"] == "https://dl.fedoraproject.org/pub/epel/RPM-GPG-KEY-EPEL-9"
    assert out["gpgcheck"] is True


def test_render_repo_applies_entry_vars():
    entry = next(r for r in rc.load_repos("rocky") if r["id"] == "kubernetes")
    out = rc.render_repo(
        entry,
        "rocky",
        releasever="9",
        basearch="x86_64",
        values={"k8s_series": "v1.31"},
    )
    assert "core:/stable:/v1.31/" in out["baseurl"]


def test_render_repo_fedora_override():
    entry = next(r for r in rc.load_repos("fedora") if r["id"] == "docker-ce")
    out = rc.render_repo(entry, "fedora", releasever="41", basearch="x86_64")
    assert "download.docker.com/linux/fedora/41/" in out["baseurl"]


def test_substitute_vars_brace_and_dollar():
    assert (
        rc.substitute_vars("rhel-{minor}-$basearch", {"minor": "9.6", "basearch": "x86_64"})
        == "rhel-9.6-x86_64"
    )


def test_supported_distros():
    distros = rc.supported_distros()
    assert "rocky" in distros
    assert "fedora" in distros


# ── Solver-verified pin set (dnf download --url --resolve) ───────────────


def _fake_dnf_run(stdout="", returncode=0, stderr=""):
    from subprocess import CompletedProcess

    return CompletedProcess(["dnf"], returncode, stdout, stderr)


def test_resolve_download_set_parses_urls(tmp_path, monkeypatch):
    stdout = "\n".join(
        [
            "Updating Subscription Management repositories.",
            "https://example.com/repo/patroni-4.1.5-1PGDG.rhel9.noarch.rpm",
            "https://example.com/repo/python3.12-psutil-6.1.1-42PGDG.rhel9.x86_64.rpm",
        ]
    )
    monkeypatch.setattr(rb, "_run", lambda cmd, timeout=900: _fake_dnf_run(stdout=stdout))
    pkgs, err = rb.resolve_download_set([], "9", "x86_64", ["patroni"])
    assert err is None
    assert pkgs == [
        "patroni-4.1.5-1PGDG.rhel9.noarch",
        "python3.12-psutil-6.1.1-42PGDG.rhel9.x86_64",
    ]


def test_resolve_download_set_failure_returns_problem(monkeypatch):
    stderr = (
        "Error in resolve\n  Problem: conflicting requests\n  - nothing provides python3.12-ydiff"
    )
    monkeypatch.setattr(
        rb, "_run", lambda cmd, timeout=900: _fake_dnf_run(stderr=stderr, returncode=1)
    )
    pkgs, err = rb.resolve_download_set([], "9", "x86_64", ["ydiff-1.4.2"])
    assert pkgs == []
    assert err is not None
    assert "nothing provides" in err


# ── Base installroot population checks ───────────────────────────────────


def test_dnf_root_populated_requires_rpmdb(tmp_path):
    assert not _dnf_root_populated(tmp_path)
    rpmdb = tmp_path / "var" / "lib" / "rpm"
    rpmdb.mkdir(parents=True)
    assert not _dnf_root_populated(tmp_path)  # empty rpmdb still counts as empty
    (rpmdb / "Packages.db").write_text("data")
    assert _dnf_root_populated(tmp_path)


def test_apt_root_populated_requires_dpkg_status(tmp_path):
    assert not _apt_root_populated(tmp_path)
    status = tmp_path / "var" / "lib" / "dpkg" / "status"
    status.parent.mkdir(parents=True)
    status.write_text("")
    assert not _apt_root_populated(tmp_path)
    status.write_text("Package: base\n")
    assert _apt_root_populated(tmp_path)


# ── Inline pip packages → requirements.txt ───────────────────────────────


def test_materialize_pip_requirements_generates_file(tmp_path):
    manifest = {
        "spec": {
            "tasks": [
                {
                    "name": "ml tools",
                    "plugin": "pip",
                    "packages": ["requests", "flask"],
                    "python_version": "3.11",
                    "_inline_packages": True,
                }
            ]
        }
    }
    save = tmp_path / "bundle.yaml"
    _materialize_pip_requirements(manifest, save)
    req_file = tmp_path / "ml-tools-requirements.txt"
    assert req_file.read_text() == "requests\nflask\n"
    task = manifest["spec"]["tasks"][0]
    assert task["requirements"] == "ml-tools-requirements.txt"
    assert "packages" not in task
    assert "_inline_packages" not in task


def test_materialize_skips_tasks_without_packages(tmp_path):
    manifest = {
        "spec": {
            "tasks": [
                {"name": "apt pkgs", "plugin": "apt", "packages": ["nginx"]},
                {"name": "pip req", "plugin": "pip", "requirements": "./req.txt"},
            ]
        }
    }
    save = tmp_path / "bundle.yaml"
    _materialize_pip_requirements(manifest, save)
    assert not (tmp_path / "apt-pkgs-requirements.txt").exists()
    assert not (tmp_path / "pip-req-requirements.txt").exists()
    assert manifest["spec"]["tasks"][0].get("packages") == ["nginx"]
