import contextlib
import shutil
import tarfile
import tempfile
import zipfile
from collections.abc import Iterator
from pathlib import Path

from syncit.bundle.bundle import read_meta


def is_archive(path: Path) -> bool:
    """Return True if path is a .tar.gz or .zip file."""
    name = path.name.lower()
    return name.endswith((".tar.gz", ".tgz", ".zip"))


def pack_archive(bundle_dir: Path, output_path: Path, fmt: str) -> Path:
    """Compress bundle_dir into output_path. Returns path to archive."""
    if fmt in ("tar.gz", "tgz"):
        archive_path = output_path.with_suffix(".tar.gz")
        with tarfile.open(archive_path, "w:gz") as tar:
            # Add the bundle_dir's contents to the root of the archive
            tar.add(bundle_dir, arcname=bundle_dir.name)
        return archive_path
    elif fmt == "zip":
        archive_path = output_path.with_suffix(".zip")
        with zipfile.ZipFile(archive_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for file_path in bundle_dir.rglob("*"):
                zf.write(file_path, file_path.relative_to(bundle_dir.parent))
        return archive_path
    else:
        raise ValueError(f"Unsupported format: {fmt}")


def extract_archive(archive_path: Path) -> Path:
    """Extract archive to a temp dir. Returns temp dir path. Caller must clean up."""
    tmp_dir = Path(tempfile.mkdtemp(prefix="syncit-bundle-"))
    if archive_path.name.lower().endswith((".tar.gz", ".tgz")):
        with tarfile.open(archive_path, "r:gz") as tar:
            import os

            def is_within_directory(directory, target):
                abs_directory = os.path.abspath(directory)
                abs_target = os.path.abspath(target)
                prefix = os.path.commonprefix([abs_directory, abs_target])
                return prefix == abs_directory

            def safe_extract(tar, path=".", members=None, *, numeric_owner=False):
                for member in tar.getmembers():
                    member_path = os.path.join(path, member.name)
                    if not is_within_directory(path, member_path):
                        raise Exception("Attempted Path Traversal in Tar File")
                tar.extractall(path, members, numeric_owner=numeric_owner, filter="data")

            safe_extract(tar, str(tmp_dir))

    elif archive_path.name.lower().endswith(".zip"):
        with zipfile.ZipFile(archive_path, "r") as zf:
            zf.extractall(tmp_dir)
    else:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise ValueError(f"Unknown archive format: {archive_path}")

    return tmp_dir


@contextlib.contextmanager
def detect_bundle(path: Path) -> Iterator[Path]:
    """
    Given a path that is either a dir or an archive,
    return the bundle directory path (extracting if needed).
    Raises ValueError if no bundle.meta.json found after extraction.
    Cleans up any temporary directories created upon context exit.
    """
    is_temp = False
    target_dir = path.resolve()

    if is_archive(target_dir):
        target_dir = extract_archive(target_dir)
        is_temp = True

        # We need to find where the bundle actually is inside the extraction.
        # It's usually the top-level directory since we pack using the bundle's name
        # as the root dir inside the tar/zip.
        dirs = list(target_dir.iterdir())
        if len(dirs) == 1 and dirs[0].is_dir():
            target_dir = dirs[0]

    try:
        try:
            read_meta(target_dir)
        except Exception as e:
            raise ValueError(f"bundle.meta.json not found in {target_dir}: {e}") from e

        yield target_dir
    finally:
        if is_temp:
            # target_dir might be a subdirectory of the actual tmp_dir
            # We want to make sure we clean up the actual mkdtemp directory!
            # Since target_dir = dirs[0], target_dir.parent would be the tmp_dir.
            # Wait, let's just use the known tmp directory pattern.
            if target_dir.parent.name.startswith("syncit-bundle-"):
                cleanup_dir = target_dir.parent
            else:
                cleanup_dir = target_dir
            shutil.rmtree(cleanup_dir, ignore_errors=True)
