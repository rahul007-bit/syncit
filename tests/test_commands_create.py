from pathlib import Path
import json
from unittest.mock import MagicMock, patch
import pytest
import typer

from syncit.commands.create import (
    _detect_codename,
    _detect_releasever,
    _detect_distro,
    _load_manifest,
    _dump_manifest,
    _build_catalog_entry,
    create_cmd,
)

def test_detect_codename(tmp_path: Path) -> None:
    # 1. Successful detect
    fake_os_release = tmp_path / "os-release"
    fake_os_release.write_text('VERSION_CODENAME="noble"\n')
    
    with patch("builtins.open", return_value=open(fake_os_release)):
        assert _detect_codename() == "noble"

    # 2. File not found
    with patch("builtins.open", side_effect=OSError):
        assert _detect_codename() == ""


def test_detect_releasever(tmp_path: Path) -> None:
    # 1. Query dnf base success
    mock_run = MagicMock(returncode=0, stdout="9\n")
    with patch("subprocess.run", return_value=mock_run):
        assert _detect_releasever() == "9"

    # 2. DNF fails, fallback to VERSION_ID in os-release
    fake_os_release = tmp_path / "os-release"
    fake_os_release.write_text('VERSION_ID="9.4"\n')
    
    def mock_run_fail(*args, **kwargs):
        raise Exception("no dnf")
        
    with patch("subprocess.run", side_effect=mock_run_fail):
        with patch("builtins.open", return_value=open(fake_os_release)):
            assert _detect_releasever() == "9"

    # 3. File not found
    with patch("subprocess.run", side_effect=mock_run_fail):
        with patch("builtins.open", side_effect=OSError):
            assert _detect_releasever() == ""


def test_detect_distro(tmp_path: Path) -> None:
    # 1. ID match
    fake_os_release = tmp_path / "os-release"
    fake_os_release.write_text('ID="ubuntu"\n')
    with patch("builtins.open", return_value=open(fake_os_release)):
        assert _detect_distro() == "Ubuntu"

    # 2. ID_LIKE match
    fake_os_release.write_text('ID="some_unknown"\nID_LIKE="rhel centos"\n')
    with patch("builtins.open", return_value=open(fake_os_release)):
        assert _detect_distro() == "RHEL"

    # 3. File not found
    with patch("builtins.open", side_effect=OSError):
        assert _detect_distro() == ""


def test_load_dump_manifest(tmp_path: Path) -> None:
    manifest_path = tmp_path / "bundle.yaml"
    data = {"apiVersion": "syncit/v1", "kind": "Bundle", "metadata": {"name": "test"}}
    _dump_manifest(data, manifest_path)
    
    loaded = _load_manifest(manifest_path)
    assert loaded == data


def test_build_catalog_entry() -> None:
    task = {"name": "test-task", "plugin": "oci_image", "images": ["alpine"]}
    entry = _build_catalog_entry(task, "oci_image")
    assert "subtasks" in entry
    assert entry["subtasks"]["packages"]["label"] == "test-task"
    assert entry["subtasks"]["packages"]["templates"]["any"]["plugin"] == "oci_image"


@patch("syncit.commands.create.get_catalog")
@patch("questionary.select")
@patch("questionary.text")
@patch("questionary.confirm")
@patch("questionary.checkbox")
def test_create_cmd_interactive_workflow(
    mock_checkbox, mock_confirm, mock_text, mock_select, mock_get_catalog, tmp_path: Path
) -> None:
    mock_checkbox.return_value = MagicMock(ask=lambda: [])
    mock_get_catalog.return_value = {
        "postgresql": {
            "category": "database",
            "description": "PostgreSQL",
            "subtasks": {
                "packages": {
                    "label": "postgresql",
                    "required": True,
                    "templates": {"any": {"name": "PostgreSQL Images", "plugin": "oci_image", "images": ["postgres:alpine"]}}
                }
            }
        }
    }

    # Simulate: 
    # - New bundle
    # - Distro: Rocky
    # - Releasever: 9
    # - Arch: amd64
    # - Add from catalog? Yes
    # Simulate Rocky distro (non-APT), no base_installroot, adding postgresql from catalog, then saving and exiting.
    mock_select.side_effect = [
        MagicMock(ask=lambda: "Rocky"),                   # Select 1: Distro Choice
        MagicMock(ask=lambda: "amd64"),                   # Select 2: Arch
        MagicMock(ask=lambda: "Search catalog"),          # Select 3: What next?
        MagicMock(ask=lambda: "postgresql"),              # Select 4: Catalog search selection
        MagicMock(ask=lambda: "latest"),                  # Select 5: Version selection
        MagicMock(ask=lambda: "Done"),                    # Select 6: What next? (breaks loop)
        MagicMock(ask=lambda: "none"),                    # Select 7: Run choice
    ]

    mock_text.side_effect = [
        MagicMock(ask=lambda: "test-rocky"),              # Text 1: Bundle Name
        MagicMock(ask=lambda: "1.0.0"),                   # Text 2: Bundle Version
        MagicMock(ask=lambda: "9"),                       # Text 3: Release version
        MagicMock(ask=lambda: str(tmp_path / "bundle.yaml")),  # Text 4: Save file path
    ]

    mock_confirm.side_effect = [
        MagicMock(ask=lambda: False),                      # Confirm 1: Enable base_installroot?
        MagicMock(ask=lambda: False),                      # Confirm 2: Add another?
    ]

    with patch("syncit.commands.create.Console.print"):
        create_cmd(None)

    save_file = tmp_path / "bundle.yaml"
    assert save_file.exists()
    
    # Read saved manifest
    loaded = _load_manifest(save_file)
    assert loaded["metadata"]["name"] == "test-rocky"
    assert loaded["spec"]["targets"]["distro"] == "rocky"
    assert loaded["spec"]["targets"]["codename"] == "9"
    assert len(loaded["spec"]["tasks"]) == 1
    assert loaded["spec"]["tasks"][0]["plugin"] == "oci_image"
