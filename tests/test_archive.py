import shutil
from pathlib import Path

import pytest

from syncit.bundle.archive import detect_bundle, extract_archive, is_archive, pack_archive


@pytest.fixture
def mock_bundle(tmp_path: Path) -> Path:
    import json
    from datetime import UTC, datetime

    bundle_dir = tmp_path / "mock-bundle"
    bundle_dir.mkdir()
    meta = {
        "name": "test",
        "version": "1.0",
        "created_at": datetime.now(UTC).isoformat(),
        "syncit_version": "0.1",
        "targets": {"distro": "u", "codename": "c", "arch": "a"},
        "tasks": [],
    }
    (bundle_dir / "bundle.meta.json").write_text(json.dumps(meta))
    (bundle_dir / "data.txt").write_text("hello offline")
    return bundle_dir


def test_is_archive() -> None:
    assert is_archive(Path("bundle.tar.gz"))
    assert is_archive(Path("bundle.tgz"))
    assert is_archive(Path("bundle.zip"))
    assert not is_archive(Path("bundle.tar"))  # Wait, I only added tar.gz and zip!
    assert not is_archive(Path("bundle_dir"))


def test_pack_and_extract_tar_gz(mock_bundle: Path, tmp_path: Path) -> None:
    output = tmp_path / "out"
    archive = pack_archive(mock_bundle, output, "tar.gz")

    assert archive.exists()
    assert archive.name == "out.tar.gz"

    extracted = extract_archive(archive)
    try:
        assert extracted.exists()
        extracted_bundle = extracted / mock_bundle.name
        assert extracted_bundle.is_dir()
        assert (extracted_bundle / "bundle.meta.json").exists()
        assert (extracted_bundle / "data.txt").read_text() == "hello offline"
    finally:
        shutil.rmtree(extracted)


def test_pack_and_extract_zip(mock_bundle: Path, tmp_path: Path) -> None:
    output = tmp_path / "out"
    archive = pack_archive(mock_bundle, output, "zip")

    assert archive.exists()
    assert archive.name == "out.zip"

    extracted = extract_archive(archive)
    try:
        assert extracted.exists()
        extracted_bundle = extracted / mock_bundle.name
        assert extracted_bundle.is_dir()
        assert (extracted_bundle / "bundle.meta.json").exists()
        assert (extracted_bundle / "data.txt").read_text() == "hello offline"
    finally:
        shutil.rmtree(extracted)


def test_detect_bundle_with_directory(mock_bundle: Path) -> None:
    with detect_bundle(mock_bundle) as bundle:
        assert bundle == mock_bundle
        assert bundle.exists()


def test_detect_bundle_with_tar_gz(mock_bundle: Path, tmp_path: Path) -> None:
    output = tmp_path / "out"
    archive = pack_archive(mock_bundle, output, "tar.gz")

    with detect_bundle(archive) as bundle:
        assert bundle.is_dir()
        assert bundle.parent.name.startswith("syncit-bundle-")  # it was extracted
        assert (bundle / "bundle.meta.json").exists()

    # ensure it gets cleaned up
    assert not bundle.exists()


def test_detect_bundle_missing_meta(tmp_path: Path) -> None:
    bundle = tmp_path / "invalid-bundle"
    bundle.mkdir()

    with pytest.raises(ValueError, match="bundle.meta.json not found"):
        with detect_bundle(bundle):
            pass
