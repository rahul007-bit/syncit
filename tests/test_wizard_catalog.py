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
    assert rc.load_repos("arch") == []
    assert rc.load_repos("") == []


def test_load_repos_ubuntu_apt_catalog():
    repos = rc.load_repos("ubuntu")
    ids = {r["id"] for r in repos}
    assert "docker-ce" in ids
    assert "kubernetes" in ids
    assert "pgdg" in ids


def test_render_apt_repo_substitutes_codename():
    entry = next(r for r in rc.load_repos("ubuntu") if r["id"] == "docker-ce")
    out = rc.render_entry_repos(entry, "ubuntu", releasever="noble", basearch="amd64")
    assert out[0]["url"] == "deb https://download.docker.com/linux/ubuntu noble stable"
    assert out[0]["gpg_key"] == "https://download.docker.com/linux/ubuntu/gpg"
    assert out[0]["name"] == "docker-ce"


def test_render_apt_repo_kubernetes_var():
    entry = next(r for r in rc.load_repos("ubuntu") if r["id"] == "kubernetes")
    out = rc.render_entry_repos(
        entry, "ubuntu", releasever="noble", basearch="amd64", values={"k8s_series": "v1.31"}
    )
    assert out[0]["url"] == "deb https://pkgs.k8s.io/core:/stable:/v1.31/deb /"


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


def test_render_entry_repos_pgdg_adds_common_and_versioned():
    entry = next(r for r in rc.load_repos("rocky") if r["id"] == "pgdg")
    out = rc.render_entry_repos(
        entry,
        "rocky",
        releasever="9",
        basearch="x86_64",
        values={"pgdg_ver": "17", "pgdg_minor": "9.6"},
    )
    names = {r["name"] for r in out}
    assert names == {"pgdg-common", "pgdg17"}
    urls = {r["baseurl"] for r in out}
    assert "https://download.postgresql.org/pub/repos/yum/common/redhat/rhel-9.6-x86_64" in urls
    assert "https://download.postgresql.org/pub/repos/yum/17/redhat/rhel-9.6-x86_64" in urls


# ── Expanded catalog: databases, messaging, big data, CI tools ───────────


def test_expanded_catalog_entries_present():
    ids = {r["id"] for r in rc.load_repos("rocky")}
    for expected in (
        "redis",
        "rabbitmq",
        "rabbitmq-erlang",
        "mysql-community",
        "mariadb",
        "clickhouse",
        "elasticsearch",
        "jenkins",
        "hadoop-bigtop",
    ):
        assert expected in ids, f"{expected} missing from catalog"


def test_mysql_community_var_render():
    entry = next(r for r in rc.load_repos("rocky") if r["id"] == "mysql-community")
    out = rc.render_entry_repos(
        entry,
        "rocky",
        releasever="9",
        basearch="x86_64",
        values={"mysql_ver": "8.4-community"},
    )
    assert out[0]["baseurl"] == "https://repo.mysql.com/yum/8.4-community/el/9/x86_64/"


def test_hadoop_bigtop_var_render():
    entry = next(r for r in rc.load_repos("rocky") if r["id"] == "hadoop-bigtop")
    out = rc.render_entry_repos(
        entry,
        "rocky",
        releasever="9",
        basearch="x86_64",
        values={"bigtop_ver": "3.6.0"},
    )
    assert out[0]["baseurl"] == "http://repos.bigtop.apache.org/releases/3.6.0/rockylinux/9/x86_64"


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


# ── Transactional co-installability check ────────────────────────────────


def test_verify_install_detects_problem_lines(monkeypatch):
    stdout = "\n".join(
        [
            "Dependencies resolved.",
            "Error:",
            " Problem: cannot install both libs-17.9 and libs-17.11",
            "  - conflicting requests",
            "Operation aborted.",
        ]
    )
    monkeypatch.setattr(
        rb, "_run", lambda cmd, timeout=900: _fake_dnf_run(stdout=stdout, returncode=1)
    )
    ok, detail = rb.verify_install([], "9", "x86_64", ["libs-17.9"])
    assert ok is False
    assert "cannot install both" in (detail or "")


def test_verify_install_treats_aborted_but_clean_as_ok(monkeypatch):
    stdout = "Dependencies resolved.\nTotal download size: 19 M\nOperation aborted.\n"
    monkeypatch.setattr(
        rb, "_run", lambda cmd, timeout=900: _fake_dnf_run(stdout=stdout, returncode=1)
    )
    ok, detail = rb.verify_install([], "9", "x86_64", ["htop"])
    assert ok is True
    assert detail is None


# ── Nevra parsing / version compare (conflict healing) ───────────────────


def test_nevra_parts():
    assert rb._nevra_parts("postgresql17-libs-17.9-1PGDG.rhel9.6.x86_64") == (
        "postgresql17-libs",
        "17.9",
        "1PGDG.rhel9.6",
    )
    assert rb._nevra_parts("docker-ce-3:29.8.1-1.el9.x86_64")[1] == "3:29.8.1"


def test_version_le():
    assert rb._version_le("17.9", "17.11") is True
    assert rb._version_le("17.11", "17.9") is False
    assert rb._version_le("1.0", "1.0") is True


# ── Host repo discovery (/etc/yum.repos.d) ───────────────────────────────


def test_load_host_repos_parses_repo_files(tmp_path):
    from syncit.wizard import host_repos as hr

    (tmp_path / "custom.repo").write_text(
        "[my-el9]\n"
        "name=My Internal EL9 Mirror\n"
        "baseurl = https://mirror.internal/el9/$releasever/$basearch/\n"
        "enabled=1\n"
        "gpgkey = file:///etc/pki/rpm-gpg/RPM-GPG-KEY-my\n"
        "gpgcheck=1\n"
        "\n"
        "[disabled-one]\n"
        "name=Disabled\n"
        "baseurl=https://example.com/disabled/\n"
        "enabled=0\n"
    )
    (tmp_path / "src.repo").write_text(
        "[appstream-source]\nname=Source\nbaseurl=https://example.com/source/\nenabled=1\n"
    )
    entries = hr.load_host_repos(tmp_path)
    ids = [e["id"] for e in entries]
    assert ids == ["my-el9"]  # disabled + -source filtered
    repo = entries[0]["repo"]
    assert repo["name"] == "my-el9"
    assert repo["baseurl"] == "https://mirror.internal/el9/$releasever/$basearch/"
    assert repo["gpgkey"] == "file:///etc/pki/rpm-gpg/RPM-GPG-KEY-my"
    assert repo["gpgcheck"] is True
    assert entries[0]["label"] == "[host] my-el9"


# ── Apt browser helpers ──────────────────────────────────────────────────


def test_inject_trusted_variants():
    from syncit.wizard.apt_browser import _inject_trusted

    assert (
        _inject_trusted("deb https://x/ubuntu noble main")
        == "deb [trusted=yes] https://x/ubuntu noble main"
    )
    line = _inject_trusted("deb https://x/ubuntu noble main", "/tmp/k.gpg")
    assert line.startswith("deb [trusted=yes signed-by=/tmp/k.gpg]")
    line2 = _inject_trusted("deb [arch=amd64] https://x noble main", "/tmp/k.gpg")
    assert line2.startswith("deb [trusted=yes signed-by=/tmp/k.gpg arch=amd64]")


def test_load_host_apt_repos_deb822(tmp_path):
    from syncit.wizard import host_repos as hr

    (tmp_path / "extra.sources").write_text(
        "Types: deb\n"
        "URIs: https://apt.example.com\n"
        "Suites: noble\n"
        "Components: main universe\n"
        "Signed-By: /usr/share/keyrings/x.gpg\n"
        "Enabled: yes\n"
        "\n"
        "Types: deb\n"
        "URIs: https://disabled.example.com\n"
        "Suites: noble\n"
        "Components: main\n"
        "Enabled: no\n"
    )
    entries = hr.load_host_apt_repos(sources_dir=tmp_path, sources_list=tmp_path / "none")
    assert len(entries) == 1
    assert entries[0]["repo"]["url"] == "deb https://apt.example.com noble main universe"
    # local keyring paths are NOT carried as gpg_key (trusted=yes covers pack)
    assert "gpg_key" not in entries[0]["repo"]


def test_load_host_repos_missing_dir(tmp_path):
    from syncit.wizard import host_repos as hr

    assert hr.load_host_repos(tmp_path / "nope") == []


def test_load_host_repos_skips_mirrorlist_only_with_flag(tmp_path):
    from syncit.wizard import host_repos as hr

    (tmp_path / "ml.repo").write_text(
        "[ml-repo]\nname=ML\nmirrorlist=https://mirrors.example.com/ml\nenabled=1\n"
    )
    entries = hr.load_host_repos(tmp_path)
    assert len(entries) == 1
    assert entries[0]["repo"].get("_mirrorlist_only") is True


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
