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
    ctx = PackContext(bundle_dir=bundle_dir, manifest_dir=tmp_path, task_slug="dnf")

    res = plugin.pack({"packages": ["nginx"]}, ctx)
    assert res.success

    assert mock_run.call_count >= 2
    calls = [c[0][0] for c in mock_run.call_args_list]
    assert any("download" in args for args in calls)
    assert any("createrepo_c" in args for args in calls)

    assert (bundle_dir / "dnf").exists()


@patch("subprocess.run")
def test_dnf_apply(mock_run, tmp_path: Path) -> None:
    mock_run.return_value = MagicMock(returncode=0)

    plugin = DnfPlugin()
    bundle_dir = tmp_path / "bundle"
    rpm_bundled = bundle_dir / "dnf"
    rpm_bundled.mkdir(parents=True)
    (rpm_bundled / "nginx.rpm").touch()

    ctx = ApplyContext(bundle_dir=bundle_dir, state_file=tmp_path / "state.json", task_slug="dnf")

    sys_rpm = tmp_path / "srv/offline/dnf/dnf"
    sys_repo = tmp_path / "etc/yum.repos.d/syncit-dnf.repo"

    original_path = Path

    class MockPath(original_path):
        def __new__(cls, *args, **kwargs):
            path_str = str(args[0])
            if path_str.startswith("/srv/offline"):
                return original_path(str(tmp_path / path_str.lstrip("/")))
            if path_str.startswith("/etc"):
                return original_path(str(tmp_path / path_str.lstrip("/")))
            return original_path(*args, **kwargs)

    with patch("syncit.plugins.dnf.Path", new=MockPath):
        res = plugin.apply({"packages": ["nginx"]}, ctx)

    assert res.success
    assert mock_run.call_count == 2

    assert sys_rpm.exists()
    assert (sys_rpm / "nginx.rpm").exists()

    assert sys_repo.exists()
    assert "gpgcheck=0" in sys_repo.read_text()


def test_dnf_validate_edge_cases() -> None:
    plugin = DnfPlugin()

    # Invalid packages field type
    with patch("shutil.which", return_value="path"):
        assert "packages" in plugin.validate({"packages": "not-a-list"})[0]

        # Invalid package item type
        errors = plugin.validate({"packages": [123]})
        assert len(errors) == 1
        assert "must be a package string" in errors[0]

        # base_installroot validation
        errors = plugin.validate({"packages": ["nginx"], "base_installroot": 123})
        assert any("base_installroot" in e for e in errors)

        # releasever validation
        errors = plugin.validate({"packages": ["nginx"], "releasever": [1, 2]})
        assert any("releasever" in e for e in errors)

        # arch validation
        errors = plugin.validate({"packages": ["nginx"], "arch": 123})
        assert any("arch" in e for e in errors)

        # repos validation
        errors = plugin.validate({"packages": ["nginx"], "repos": "not-a-list"})
        assert any("repos" in e for e in errors)

        errors = plugin.validate({"packages": ["nginx"], "repos": ["not-a-dict"]})
        assert any("repos[0] must be an object" in e for e in errors)

        errors = plugin.validate({"packages": ["nginx"], "repos": [{"name": 123, "baseurl": "http://foo"}]})
        assert any("missing required string field: 'name'" in e for e in errors)

        errors = plugin.validate({"packages": ["nginx"], "repos": [{"name": "repo", "baseurl": 123}]})
        assert any("missing required string field: 'baseurl'" in e for e in errors)


def test_dnf_validate_os_warnings() -> None:
    plugin = DnfPlugin()

    # Test /etc/os-release warnings
    with patch("shutil.which", return_value="path"):
        with patch("syncit.plugins.dnf.Path.exists", return_value=True):
            with patch("syncit.plugins.dnf.Path.read_text", return_value="ID=ubuntu\n"):
                with patch("syncit.plugins.dnf.err_console.print") as mock_print:
                    plugin.validate({"packages": ["nginx"]})
                    assert mock_print.called
                    assert "Host OS may not be RHEL" in mock_print.call_args[0][0]

        with patch("syncit.plugins.dnf.Path.exists", return_value=False):
            with patch("syncit.plugins.dnf.err_console.print") as mock_print:
                plugin.validate({"packages": ["nginx"]})
                assert mock_print.called
                assert "/etc/os-release not found" in mock_print.call_args[0][0]


def test_dnf_validate_gpgkey_download_missing() -> None:
    plugin = DnfPlugin()

    # Test curl and wget missing when gpgkey is specified
    with patch("shutil.which", side_effect=lambda x: "path" if x in ["dnf", "createrepo_c"] else None):
        errors = plugin.validate({
            "packages": ["nginx"],
            "repos": [{"name": "foo", "baseurl": "http://bar", "gpgkey": "http://key"}]
        })
        assert any("has 'gpgkey' but neither 'curl' nor 'wget' is installed" in e for e in errors)


def test_dnf_diff() -> None:
    plugin = DnfPlugin()

    # Old spec is None
    diff_res = plugin.diff(None, {"packages": ["nginx"], "repos": [{"name": "foo", "baseurl": "http://bar"}]})
    assert "[repo] foo (http://bar)" in diff_res.added
    assert "nginx" in diff_res.added
    assert not diff_res.removed

    # Comparing specs with added and removed packages/repos
    old_spec = {
        "packages": ["nginx", "curl"],
        "repos": [{"name": "foo", "baseurl": "http://bar"}]
    }
    new_spec = {
        "packages": ["nginx", "wget"],
        "repos": [{"name": "baz", "baseurl": "http://qux"}]
    }
    diff_res = plugin.diff(old_spec, new_spec)
    assert "[repo] baz (http://qux)" in diff_res.added
    assert "wget" in diff_res.added
    assert "[repo] foo (http://bar)" in diff_res.removed
    assert "curl" in diff_res.removed


def test_dnf_render_apply_sh() -> None:
    plugin = DnfPlugin()
    script = plugin.render_apply_sh({"packages": ["nginx", "curl"]}, "dnf/install-web")
    assert "dnf install -y --disablerepo=* --enablerepo=syncit-install-web nginx curl" in script
    assert "mkdir -p /srv/offline/dnf/install-web" in script


@patch("syncit.plugins.dnf._run_cmd")
@patch("subprocess.run")
def test_dnf_pack_caching_hit_and_atomic_promotion(mock_sub_run, mock_run_cmd, tmp_path: Path, monkeypatch) -> None:
    cache_root = tmp_path / "cache"
    dnf_cache = cache_root / "dnf"
    rpm_cache = cache_root / "dnf-rpms"
    dnf_cache.mkdir(parents=True)
    rpm_cache.mkdir(parents=True)

    monkeypatch.setattr("syncit.plugins.dnf.Path.expanduser", lambda self: cache_root / self.name if "cache/syncit" in str(self) else self)

    # Pre-populate cache with valid nginx.rpm
    cached_rpm = rpm_cache / "nginx-1.20.rpm"
    cached_rpm.write_text("valid-rpm-content")

    # Mock subprocess.run for url check and rpm -K
    def side_effect_sub_run(cmd, *args, **kwargs):
        if "--url" in cmd:
            return MagicMock(returncode=0, stdout="https://mirror.example.com/nginx-1.20.rpm\n")
        if "rpm" in cmd and "-K" in cmd:
            return MagicMock(returncode=0)  # valid integrity
        return MagicMock(returncode=0)

    mock_sub_run.side_effect = side_effect_sub_run

    # When _run_cmd is called (for dnf download), simulate downloading a new package (curl.rpm)
    def side_effect_run_cmd(cmd, *args, **kwargs):
        if "download" in cmd:
            dest_idx = cmd.index("--destdir") + 1
            dest_dir = Path(cmd[dest_idx])
            (dest_dir / "curl-7.76.rpm").write_text("new-curl-content")
        return MagicMock(returncode=0)

    mock_run_cmd.side_effect = side_effect_run_cmd

    plugin = DnfPlugin()
    bundle_dir = tmp_path / "bundle"
    ctx = PackContext(bundle_dir=bundle_dir, manifest_dir=tmp_path, task_slug="dnf", verbose=True)

    res = plugin.pack({"packages": ["nginx", "curl"]}, ctx)
    assert res.success

    # Verify cached nginx-1.20.rpm was copied to bundle
    assert (bundle_dir / "dnf/nginx-1.20.rpm").exists()
    # Verify new curl-7.76.rpm was atomically promoted to permanent cache and copied to bundle
    assert (rpm_cache / "curl-7.76.rpm").exists()
    assert (rpm_cache / "curl-7.76.rpm").read_text() == "new-curl-content"
    assert (bundle_dir / "dnf/curl-7.76.rpm").exists()


@patch("syncit.plugins.dnf._run_cmd")
@patch("subprocess.run")
def test_dnf_pack_caching_corrupted_removal(mock_sub_run, mock_run_cmd, tmp_path: Path, monkeypatch) -> None:
    cache_root = tmp_path / "cache"
    rpm_cache = cache_root / "dnf-rpms"
    rpm_cache.mkdir(parents=True)

    monkeypatch.setattr("syncit.plugins.dnf.Path.expanduser", lambda self: cache_root / self.name if "cache/syncit" in str(self) else self)

    # Pre-populate cache with corrupted rpm
    corrupted_rpm = rpm_cache / "bad-pkg.rpm"
    corrupted_rpm.write_text("corrupted-data")

    def side_effect_sub_run(cmd, *args, **kwargs):
        if "--url" in cmd:
            return MagicMock(returncode=0, stdout="https://mirror.example.com/bad-pkg.rpm\n")
        if "rpm" in cmd and "-K" in cmd:
            return MagicMock(returncode=1)  # failed integrity!
        return MagicMock(returncode=0)

    mock_sub_run.side_effect = side_effect_sub_run
    mock_run_cmd.return_value = MagicMock(returncode=0)

    plugin = DnfPlugin()
    bundle_dir = tmp_path / "bundle"
    ctx = PackContext(bundle_dir=bundle_dir, manifest_dir=tmp_path, task_slug="dnf", verbose=True)

    res = plugin.pack({"packages": ["bad-pkg"]}, ctx)
    assert res.success

    # Corrupted file in cache should have been removed!
    assert not corrupted_rpm.exists()


@patch("syncit.plugins.dnf._run_cmd")
@patch("subprocess.run")
def test_dnf_pack_no_cache_clears_dirs(mock_sub_run, mock_run_cmd, tmp_path: Path, monkeypatch) -> None:
    cache_root = tmp_path / "cache"
    dnf_cache = cache_root / "dnf"
    rpm_cache = cache_root / "dnf-rpms"
    dnf_cache.mkdir(parents=True)
    rpm_cache.mkdir(parents=True)
    (dnf_cache / "old-meta").touch()
    (rpm_cache / "old.rpm").touch()

    monkeypatch.setattr("syncit.plugins.dnf.Path.expanduser", lambda self: cache_root / self.name if "cache/syncit" in str(self) else self)
    mock_sub_run.return_value = MagicMock(returncode=0, stdout="")
    mock_run_cmd.return_value = MagicMock(returncode=0)

    plugin = DnfPlugin()
    bundle_dir = tmp_path / "bundle"
    ctx = PackContext(bundle_dir=bundle_dir, manifest_dir=tmp_path, task_slug="dnf", no_cache=True, verbose=True)

    res = plugin.pack({"packages": ["nginx"]}, ctx)
    assert res.success
    # Old cache files should be wiped
    assert not (dnf_cache / "old-meta").exists()
    assert not (rpm_cache / "old.rpm").exists()


@patch("syncit.plugins.dnf._run_cmd")
@patch("subprocess.run")
def test_dnf_pack_installroot_copy_pki(mock_sub_run, mock_run_cmd, tmp_path: Path) -> None:
    mock_sub_run.return_value = MagicMock(returncode=0, stdout="")
    mock_run_cmd.return_value = MagicMock(returncode=0)

    installroot = tmp_path / "ir"
    installroot.mkdir()
    
    # Create fake host PKI directory
    host_gpg = tmp_path / "etc/pki/rpm-gpg"
    host_gpg.mkdir(parents=True)
    (host_gpg / "RPM-GPG-KEY-test").touch()

    original_path = Path
    class MockPath(original_path):
        def __new__(cls, *args, **kwargs):
            path_str = str(args[0])
            if path_str in ["/etc/pki/entitlement", "/etc/rhsm", "/etc/yum.repos.d", "/etc/pki/rpm-gpg"]:
                if path_str == "/etc/pki/rpm-gpg":
                    return original_path(str(host_gpg))
                return original_path(str(tmp_path / "nonexistent"))
            return original_path(*args, **kwargs)

    with patch("syncit.plugins.dnf.Path", new=MockPath):
        plugin = DnfPlugin()
        bundle_dir = tmp_path / "bundle"
        ctx = PackContext(bundle_dir=bundle_dir, manifest_dir=tmp_path, task_slug="dnf", verbose=True)
        res = plugin.pack({"packages": ["nginx"], "base_installroot": str(installroot)}, ctx)

    assert res.success
    # Verify RPM-GPG-KEY-test was copied into installroot
    assert (installroot / "etc/pki/rpm-gpg/RPM-GPG-KEY-test").exists()


@patch("subprocess.Popen")
def test_run_cmd_verbose_streaming(mock_popen) -> None:
    from syncit.plugins.dnf import _run_cmd
    
    mock_proc = MagicMock()
    mock_proc.stdout = ["line1\n", "line2\n"]
    mock_proc.returncode = 0
    mock_popen.return_value = mock_proc

    res = _run_cmd(["dnf", "download"], verbose=True)
    assert res.returncode == 0
    assert res.stdout == "line1\nline2\n"
    assert mock_popen.called


def test_dnf_pack_empty_packages(tmp_path: Path) -> None:
    plugin = DnfPlugin()
    ctx = PackContext(bundle_dir=tmp_path / "bundle", manifest_dir=tmp_path)
    res = plugin.pack({"packages": []}, ctx)
    assert res.success
    assert "No dnf tasks" in res.message


@patch("syncit.plugins.dnf._run_cmd")
@patch("subprocess.run")
def test_dnf_pack_with_repos(mock_sub_run, mock_run_cmd, tmp_path: Path) -> None:
    mock_sub_run.return_value = MagicMock(returncode=0, stdout="")
    mock_run_cmd.return_value = MagicMock(returncode=0)

    plugin = DnfPlugin()
    bundle_dir = tmp_path / "bundle"
    ctx = PackContext(bundle_dir=bundle_dir, manifest_dir=tmp_path, task_slug="dnf", verbose=True)

    spec = {
        "packages": ["nginx"],
        "repos": [{"name": "custom-repo", "baseurl": "https://example.com/repo", "gpgkey": "https://example.com/key"}]
    }
    res = plugin.pack(spec, ctx)
    assert res.success
    assert (bundle_dir / "dnf" / "repos.json").exists()


@patch("syncit.plugins.dnf._run_cmd")
@patch("subprocess.run")
@patch("sys.stdout.isatty", return_value=True)
@patch("questionary.confirm")
def test_dnf_pack_rhel_fallback_retry(mock_confirm, mock_isatty, mock_sub_run, mock_run_cmd, tmp_path: Path) -> None:
    # First download fails with "nothing provides", retry succeeds, createrepo succeeds
    mock_run_cmd.side_effect = [
        MagicMock(returncode=1, stderr="nothing provides some-dep"),
        MagicMock(returncode=0), # Retry succeeds
        MagicMock(returncode=0)  # createrepo_c succeeds
    ]
    mock_sub_run.return_value = MagicMock(returncode=0, stdout="")
    mock_confirm.return_value = MagicMock(ask=lambda: True)

    plugin = DnfPlugin()
    bundle_dir = tmp_path / "bundle"
    # Ensure targets distro is RHEL
    ctx = PackContext(bundle_dir=bundle_dir, manifest_dir=tmp_path, task_slug="dnf", verbose=True, targets={"distro": "rhel"})

    res = plugin.pack({"packages": ["nginx"]}, ctx)
    assert res.success
    assert mock_run_cmd.call_count == 3
    # Verify fallback baseos repofrompath was passed in the second run
    retry_cmd = mock_run_cmd.call_args_list[1][0][0]
    assert "syncit_fallback_baseos" in retry_cmd


@patch("syncit.plugins.dnf._run_cmd")
@patch("subprocess.run")
def test_dnf_pack_createrepo_fail(mock_sub_run, mock_run_cmd, tmp_path: Path) -> None:
    mock_sub_run.return_value = MagicMock(returncode=0, stdout="")
    # First command (dnf download) succeeds, second command (createrepo_c) fails
    mock_run_cmd.side_effect = [
        MagicMock(returncode=0),
        MagicMock(returncode=1, stderr="createrepo failed")
    ]

    plugin = DnfPlugin()
    bundle_dir = tmp_path / "bundle"
    ctx = PackContext(bundle_dir=bundle_dir, manifest_dir=tmp_path, task_slug="dnf", verbose=True)

    res = plugin.pack({"packages": ["nginx"]}, ctx)
    assert not res.success
    assert "createrepo_c failed" in res.errors[0]


def test_dnf_apply_empty_packages(tmp_path: Path) -> None:
    plugin = DnfPlugin()
    ctx = ApplyContext(bundle_dir=tmp_path / "bundle", state_file=tmp_path / "state.json")
    res = plugin.apply({"packages": []}, ctx)
    assert res.success
    assert "No dnf tasks" in res.message


def test_dnf_apply_missing_bundle(tmp_path: Path) -> None:
    plugin = DnfPlugin()
    ctx = ApplyContext(bundle_dir=tmp_path / "nonexistent", state_file=tmp_path / "state.json", task_slug="dnf")
    res = plugin.apply({"packages": ["nginx"]}, ctx)
    assert not res.success
    assert "No bundled RPMs found" in res.message


@patch("syncit.plugins.dnf._run_cmd")
def test_dnf_apply_createrepo_fail(mock_run_cmd, tmp_path: Path) -> None:
    mock_run_cmd.return_value = MagicMock(returncode=1, stderr="failed to run createrepo")

    plugin = DnfPlugin()
    bundle_dir = tmp_path / "bundle"
    (bundle_dir / "dnf").mkdir(parents=True)
    ctx = ApplyContext(bundle_dir=bundle_dir, state_file=tmp_path / "state.json", task_slug="dnf")

    original_path = Path
    class MockPath(original_path):
        def __new__(cls, *args, **kwargs):
            path_str = str(args[0])
            if path_str.startswith("/srv/offline") or path_str.startswith("/etc"):
                return original_path(str(tmp_path / path_str.lstrip("/")))
            return original_path(*args, **kwargs)

    with patch("syncit.plugins.dnf.Path", new=MockPath):
        res = plugin.apply({"packages": ["nginx"]}, ctx)
    assert not res.success
    assert "failed to run createrepo" in res.errors[0]


@patch("syncit.plugins.dnf._run_cmd")
def test_dnf_apply_install_fail(mock_run_cmd, tmp_path: Path) -> None:
    # First command (createrepo_c) succeeds, second (dnf install) fails
    mock_run_cmd.side_effect = [
        MagicMock(returncode=0),
        MagicMock(returncode=1, stderr="dnf install error")
    ]

    plugin = DnfPlugin()
    bundle_dir = tmp_path / "bundle"
    (bundle_dir / "dnf").mkdir(parents=True)
    ctx = ApplyContext(bundle_dir=bundle_dir, state_file=tmp_path / "state.json", task_slug="dnf")

    original_path = Path
    class MockPath(original_path):
        def __new__(cls, *args, **kwargs):
            path_str = str(args[0])
            if path_str.startswith("/srv/offline") or path_str.startswith("/etc"):
                return original_path(str(tmp_path / path_str.lstrip("/")))
            return original_path(*args, **kwargs)

    with patch("syncit.plugins.dnf.Path", new=MockPath):
        res = plugin.apply({"packages": ["nginx"]}, ctx)
    assert not res.success
    assert "dnf install error" in res.errors[0]

