"""oci_image plugin — saves Docker/OCI images using skopeo and loads them offline."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

from syncit.plugins.base import (
    ApplyContext,
    DiffResult,
    OfflinePlugin,
    PackContext,
    PluginResult,
)
from syncit.plugins.registry import registry


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
    """Get image digest using skopeo inspect or podman."""
    if _has_cmd("skopeo"):
        result = _run(["skopeo", "inspect", f"docker://{source}"])
        if result.returncode == 0:
            try:
                data = json.loads(result.stdout)
                return data.get("Digest", "sha256:unknown")
            except (json.JSONDecodeError, KeyError):
                pass
    elif _has_cmd("podman"):
        result = _run(["podman", "inspect", source])
        if result.returncode == 0:
            try:
                data = json.loads(result.stdout)
                if data and isinstance(data, list):
                    return data[0].get("Digest", "sha256:unknown")
            except (json.JSONDecodeError, KeyError, IndexError):
                pass
    elif _has_cmd("docker"):
        result = _run(["docker", "inspect", source])
        if result.returncode == 0:
            try:
                data = json.loads(result.stdout)
                if data and isinstance(data, list):
                    # Docker usually doesn't show Digest in inspect for local pulls without --format,
                    # but we can try repo digests.
                    repo_digests = data[0].get("RepoDigests", [])
                    if repo_digests:
                        return repo_digests[0].split("@")[-1]
            except (json.JSONDecodeError, KeyError, IndexError):
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

        # Check for skopeo, docker, or podman at validate time
        if not _has_cmd("skopeo") and not _has_cmd("podman") and not _has_cmd("docker"):
            errors.append(
                "[oci_image] Neither 'skopeo', 'docker', nor 'podman' found in PATH — required for packing OCI images"
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

        if not _has_cmd("skopeo") and not _has_cmd("podman") and not _has_cmd("docker"):
            return PluginResult(
                success=False,
                message="[oci_image] No packing tool found",
                artifacts=[],
                errors=["Install docker, podman, or skopeo on the online VM before packing OCI images"],
            )

        image_dir.mkdir(parents=True, exist_ok=True)
        manifest_data: list[dict[str, str]] = []
        errors: list[str] = []

        for img in images:
            source = img["source"]
            safe = _safe_name(source)
            archive = image_dir / f"{safe}.tar"

            import hashlib

            cache_root = Path("~/.cache/syncit/oci_image").expanduser()
            cache_root.mkdir(parents=True, exist_ok=True)

            # Cache path based on hash of source name
            source_hash = hashlib.sha256(source.encode()).hexdigest()[:12]
            cache_path = cache_root / source_hash

            if ctx.no_cache:
                if ctx.verbose:
                    print(f"[oci_image] --no-cache: clearing cache for {source}...")
                shutil.rmtree(cache_path, ignore_errors=True)

            # 1. Pull and bundle
            # Prefer docker, then podman, then skopeo based on user preference
            pack_tool = "docker" if _has_cmd("docker") else "podman" if _has_cmd("podman") else "skopeo"

            if pack_tool == "skopeo":
                if not cache_path.exists():
                    if ctx.verbose:
                        print(f"[oci_image] Pulling {source} to cache (OCI layout)...")
                    cache_path.mkdir(parents=True, exist_ok=True)
                    pull_cmd = ["skopeo", "copy", f"docker://{source}", f"oci:{cache_path}"]
                    pull_res = _run(pull_cmd)
                    if pull_res.returncode != 0:
                        errors.append(f"[oci_image] Failed to pull {source}: {pull_res.stderr.strip()}")
                        shutil.rmtree(cache_path, ignore_errors=True)
                        continue

                if ctx.verbose:
                    print(f"[oci_image] Bundling {source} from cache layout...")
                bundle_cmd = ["skopeo", "copy", f"oci:{cache_path}", f"oci-archive:{archive}"]
                bundle_res = _run(bundle_cmd)
                if bundle_res.returncode != 0:
                    errors.append(f"[oci_image] Failed to bundle {source}: {bundle_res.stderr.strip()}")
                    continue
            else:
                # Fallback to docker or podman
                if ctx.verbose:
                    print(f"[oci_image] Pulling {source} via {pack_tool}...")
                pull_res = _run([pack_tool, "pull", source])
                if pull_res.returncode != 0:
                    errors.append(f"[oci_image] Failed to pull {source}: {pull_res.stderr.strip()}")
                    continue
                
                if ctx.verbose:
                    print(f"[oci_image] Bundling {source} via {pack_tool} save...")
                # podman supports --format=oci-archive, docker does not.
                if pack_tool == "podman":
                    bundle_cmd = [pack_tool, "save", "--format=oci-archive", "-o", str(archive), source]
                else:
                    bundle_cmd = [pack_tool, "save", "-o", str(archive), source]
                    
                bundle_res = _run(bundle_cmd)
                if bundle_res.returncode != 0:
                    errors.append(f"[oci_image] Failed to bundle {source}: {bundle_res.stderr.strip()}")
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

            # Prefer skopeo copy for loading — it correctly maps the full
            # reference (including registry prefix) into the runtime store,
            # avoiding the <none>:<none> issue that `podman/docker load` can
            # produce with oci-archive tars.
            if _has_cmd("skopeo") and runtime in ("docker", "podman"):
                storage_driver = "containers-storage" if runtime == "podman" else "docker-daemon"
                cmd = [
                    "skopeo", "copy",
                    f"oci-archive:{archive}",
                    f"{storage_driver}:{source}",
                ]
                result = _run(cmd)
                if result.returncode != 0:
                    errors.append(
                        f"[oci_image] skopeo copy failed for '{source}': {result.stderr.strip()}\n"
                        f"Falling back to {runtime} load + tag..."
                    )
                    # Fallback: runtime load then explicit tag
                    result = self._load_and_tag(runtime, archive, source, ctx.verbose)
                    if result:
                        errors.append(result)
            else:
                # ctr or no skopeo: runtime native load then explicit tag
                err = self._load_and_tag(runtime, archive, source, ctx.verbose)
                if err:
                    errors.append(err)

            # Verify image is now accessible under expected name
            if not self._image_exists(runtime, source, ""):
                errors.append(
                    f"[oci_image] WARNING: '{source}' loaded but not found under that name. "
                    f"Verify with: {runtime} images | grep {source.split('/')[-1].split(':')[0]}"
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

    def _load_and_tag(self, runtime: str, archive: Path, source: str, verbose: bool) -> str | None:
        """Load an oci-archive with runtime load, then explicitly tag it with the source reference.

        This is a fallback for when skopeo is not available. `podman/docker load`
        may drop the registry prefix from the reference, leaving images as
        <none>:<none> or with a truncated name. We fix this by tagging explicitly.
        Returns an error string on failure, or None on success.
        """
        if runtime == "docker":
            load_cmd = ["docker", "load", "-i", str(archive)]
        elif runtime == "podman":
            load_cmd = ["podman", "load", "-i", str(archive)]
        else:  # ctr
            load_cmd = ["ctr", "images", "import", str(archive)]
            result = _run(load_cmd)
            if result.returncode != 0:
                return f"[oci_image] ctr import failed for '{source}': {result.stderr.strip()}"
            return None

        result = _run(load_cmd)
        if result.returncode != 0:
            return f"[oci_image] {runtime} load failed for '{source}': {result.stderr.strip()}"

        # Extract the loaded image name from output (e.g. "Loaded image: sha256:abc..."
        # or "Loaded image ID: sha256:abc..." or the tag podman printed)
        loaded_ref: str | None = None
        for line in result.stdout.splitlines():
            line = line.strip()
            if line.startswith("Loaded image:"):
                loaded_ref = line.split(":", 1)[1].strip()
                break
            if line.startswith("Loaded image ID:"):
                loaded_ref = line.split(":", 2)[-1].strip()  # sha256 hash
                break

        if loaded_ref and loaded_ref != source:
            # The runtime gave the image a different name (or a raw sha256).
            # Explicitly tag it with the expected source reference.
            if verbose:
                print(f"[oci_image] Tagging '{loaded_ref}' → '{source}'")
            tag_cmd = [runtime, "tag", loaded_ref, source]
            tag_result = _run(tag_cmd)
            if tag_result.returncode != 0:
                return (
                    f"[oci_image] Loaded '{source}' but failed to tag: {tag_result.stderr.strip()}. "
                    f"Image may be available as '{loaded_ref}' instead."
                )

        return None


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
        # Read the manifest entries so we can generate explicit skopeo/tag commands
        # rather than a generic glob loop. This is critical to avoid <none>:<none>
        # images caused by `podman load` dropping registry prefixes from oci-archives.
        images: list[dict] = task_spec.get("images", [])

        load_lines: list[str] = []
        for img in images:
            source = img["source"]
            safe = _safe_name(source)
            tar = f"$BUNDLE_DIR/{bundle_subdir}/images/{safe}.tar"
            # skopeo path (preferred — correctly maps full reference into runtime store)
            load_lines.append(
                f'  if command -v skopeo &>/dev/null; then\n'
                f'    echo "  [oci_image] → skopeo copy {source}"\n'
                f'    skopeo copy "oci-archive:{tar}" "$STORAGE_PREFIX:{source}"\n'
                f'  else\n'
                f'    echo "  [oci_image] → $RUNTIME load {source}"\n'
                f'    LOADED=$($RUNTIME load -i "{tar}" 2>&1 | grep -oP "(?<=Loaded image: ).*" | head -1 || true)\n'
                f'    [ -n "$LOADED" ] && [ "$LOADED" != "{source}" ] && $RUNTIME tag "$LOADED" "{source}" || true\n'
                f'  fi'
            )

        per_image_block = "\n".join(load_lines)

        return f"""
echo "[oci_image] Loading container images..."
if command -v docker &>/dev/null; then
    RUNTIME="docker"
    STORAGE_PREFIX="docker-daemon"
elif command -v podman &>/dev/null; then
    RUNTIME="podman"
    STORAGE_PREFIX="containers-storage"
elif command -v ctr &>/dev/null; then
    RUNTIME="ctr"
    STORAGE_PREFIX=""
else
    echo "[oci_image] ERROR: No container runtime found (docker, podman, ctr)"
    exit 1
fi

# Load each image individually with its exact reference preserved.
# Using skopeo copy (preferred) avoids the <none>:<none> tag problem
# that occurs when podman/docker load drops registry prefixes from
# oci-archive tars. Falls back to runtime load + explicit tag if
# skopeo is not available.
{per_image_block}
"""


registry.register(OciImagePlugin())
