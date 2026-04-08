"""oci_image plugin — saves Docker/OCI images using skopeo and loads them offline."""

from __future__ import annotations

import json
import shutil
import subprocess
from typing import Any

from offlinectl.plugins.base import (
    ApplyContext,
    DiffResult,
    OfflinePlugin,
    PackContext,
    PluginResult,
)
from offlinectl.plugins.registry import registry


def _run(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, **kwargs)


def _has_cmd(name: str) -> bool:
    return shutil.which(name) is not None


def _safe_name(source: str) -> str:
    """Convert image reference to safe filename: slashes and colons → underscores."""
    return source.replace("/", "_").replace(":", "_")


def _detect_runtime() -> str | None:
    """Return the first available container runtime from PATH."""
    for rt in ("docker", "podman", "ctr"):
        if _has_cmd(rt):
            return rt
    return None


def _get_digest(source: str) -> str:
    """Get image digest using skopeo inspect."""
    result = _run(["skopeo", "inspect", f"docker://{source}"])
    if result.returncode == 0:
        try:
            data = json.loads(result.stdout)
            return data.get("Digest", "sha256:unknown")
        except (json.JSONDecodeError, KeyError):
            pass
    return "sha256:unknown"


class OciImagePlugin(OfflinePlugin):
    name = "oci_image"

    def validate(self, task_spec: dict[str, Any]) -> list[str]:
        errors: list[str] = []
        images = task_spec.get("images")
        if not images or not isinstance(images, list):
            errors.append("[oci_image] 'images' must be a non-empty list")
            return errors
        for i, img in enumerate(images):
            if not isinstance(img, dict) or "source" not in img:
                errors.append(f"[oci_image] Image at index {i} must have a 'source' key")

        # Check for skopeo at validate time
        if not _has_cmd("skopeo"):
            errors.append(
                "[oci_image] 'skopeo' not found in PATH — required for packing OCI images"
            )
        return errors

    def pack(self, task_spec: dict[str, Any], ctx: PackContext) -> PluginResult:
        images: list[dict[str, str]] = task_spec.get("images", [])
        image_dir = ctx.bundle_dir / "images"

        if ctx.dry_run:
            sources = [img["source"] for img in images]
            return PluginResult(
                success=True,
                message=f"[dry-run] Would pull {len(images)} image(s): {sources}",
                artifacts=[],
                errors=[],
            )

        if not _has_cmd("skopeo"):
            return PluginResult(
                success=False,
                message="[oci_image] skopeo not found in PATH",
                artifacts=[],
                errors=["Install skopeo on the online VM before packing OCI images"],
            )

        image_dir.mkdir(parents=True, exist_ok=True)
        manifest_data: list[dict[str, str]] = []
        errors: list[str] = []

        for img in images:
            source = img["source"]
            safe = _safe_name(source)
            archive = image_dir / f"{safe}.tar"

            cmd = ["skopeo", "copy", f"docker://{source}", f"oci-archive:{archive}"]
            result = _run(cmd)
            if result.returncode != 0:
                err = (
                    f"[oci_image] skopeo copy failed for '{source}'\n"
                    f"Command: {' '.join(cmd)}\n{result.stderr.strip()}"
                )
                errors.append(err)
                continue

            digest = _get_digest(source)
            manifest_data.append(
                {
                    "source": source,
                    "archive": f"{safe}.tar",
                    "digest": digest,
                }
            )

        # Write manifest.json
        manifest_file = image_dir / "manifest.json"
        manifest_file.write_text(json.dumps(manifest_data, indent=2))

        success = len(errors) == 0
        return PluginResult(
            success=success,
            message=(
                f"[oci_image] Packed {len(manifest_data)} image(s)"
                + (f", {len(errors)} failed" if errors else "")
            ),
            artifacts=[str(image_dir)],
            errors=errors,
        )

    def apply(self, task_spec: dict[str, Any], ctx: ApplyContext) -> PluginResult:
        image_dir = ctx.bundle_dir / "images"
        manifest_file = image_dir / "manifest.json"

        if not manifest_file.exists():
            return PluginResult(
                success=False,
                message="[oci_image] bundle/images/manifest.json not found",
                artifacts=[],
                errors=["manifest.json missing from bundle"],
            )

        manifest: list[dict[str, str]] = json.loads(manifest_file.read_text())

        runtime = _detect_runtime()

        if ctx.dry_run:
            rt_label = runtime or "no runtime detected"
            return PluginResult(
                success=True,
                message=f"[dry-run] Would load {len(manifest)} image(s) using {rt_label}",
                artifacts=[],
                errors=[],
            )

        if not runtime:
            return PluginResult(
                success=False,
                message="[oci_image] No container runtime found (checked: docker, podman, ctr)",
                artifacts=[],
                errors=["Install docker, podman, or containerd on the offline VM"],
            )

        errors: list[str] = []
        for entry in manifest:
            archive = image_dir / entry["archive"]
            source = entry["source"]

            if not archive.exists():
                errors.append(f"[oci_image] Archive missing: {archive}")
                continue

            # Idempotence: check if image with this digest already exists
            if self._image_exists(runtime, source, entry.get("digest", "")):
                if ctx.verbose:
                    print(f"[oci_image] Skipping already-loaded: {source}")
                continue

            if runtime == "docker":
                cmd = ["docker", "load", "-i", str(archive)]
            elif runtime == "podman":
                cmd = ["podman", "load", "-i", str(archive)]
            else:  # ctr
                cmd = ["ctr", "images", "import", str(archive)]

            result = _run(cmd)
            if result.returncode != 0:
                errors.append(
                    f"[oci_image] Failed to load '{source}'\n"
                    f"Command: {' '.join(cmd)}\n{result.stderr.strip()}"
                )

        success = len(errors) == 0
        return PluginResult(
            success=success,
            message=(
                f"[oci_image] Loaded images using {runtime}"
                if success
                else f"[oci_image] {len(errors)} image(s) failed to load"
            ),
            artifacts=[str(image_dir)],
            errors=errors,
        )

    def _image_exists(self, runtime: str, source: str, digest: str) -> bool:
        """Check if an image is already present in the local runtime."""
        try:
            if runtime == "docker":
                result = _run(["docker", "images", "--format", "{{.Repository}}:{{.Tag}}"])
                # Simple heuristic: check if source tag appears in output
                return source in result.stdout
            elif runtime == "podman":
                result = _run(["podman", "images", "--format", "{{.Repository}}:{{.Tag}}"])
                return source in result.stdout
        except Exception:
            pass
        return False

    def diff(self, old_spec: dict[str, Any] | None, new_spec: dict[str, Any]) -> DiffResult:
        if old_spec is None:
            new_imgs = [img["source"] for img in new_spec.get("images", [])]
            return DiffResult(
                plugin_name=self.name, added=new_imgs, removed=[], updated=[], unchanged=[]
            )

        old_map = {img["source"]: img for img in old_spec.get("images", [])}
        new_map = {img["source"]: img for img in new_spec.get("images", [])}

        old_keys = set(old_map)
        new_keys = set(new_map)

        added = sorted(new_keys - old_keys)
        removed = sorted(old_keys - new_keys)
        unchanged = sorted(old_keys & new_keys)

        return DiffResult(
            plugin_name=self.name,
            added=added,
            removed=removed,
            updated=[],  # Phase 1: no version tracking within same reference
            unchanged=unchanged,
        )

    def render_apply_sh(self, task_spec: dict[str, Any], bundle_subdir: str) -> str:
        return f"""
echo "[oci_image] Loading container images..."
if command -v docker &> /dev/null; then
    RUNTIME="docker"
    LOAD_ARGS="load -i"
elif command -v podman &> /dev/null; then
    RUNTIME="podman"
    LOAD_ARGS="load -i"
elif command -v ctr &> /dev/null; then
    RUNTIME="ctr"
    LOAD_ARGS="images import"
else
    echo "[oci_image] ERROR: No container runtime found (docker, podman, ctr)"
    exit 1
fi

for archive in $BUNDLE_DIR/{bundle_subdir}/images/*.tar; do
    if [ -f "$archive" ]; then
        echo "  → Loading $archive..."
        $RUNTIME $LOAD_ARGS "$archive"
    fi
done
"""


registry.register(OciImagePlugin())
