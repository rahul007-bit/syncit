"""File plugin for downloading and applying arbitrary files/binaries."""

from __future__ import annotations

import shutil
import urllib.request
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


class FilePlugin(OfflinePlugin):
    name = "file"

    def validate(self, task_spec: dict[str, Any]) -> list[str]:
        errors = []
        files = task_spec.get("files", [])
        if not isinstance(files, list):
            return ["[file] 'files' must be a list of file download specifications"]

        for idx, f in enumerate(files):
            if not isinstance(f, dict):
                errors.append(f"[file] item {idx} must be a dictionary")
                continue
            if "url" not in f:
                errors.append(f"[file] item {idx} missing 'url'")
            if "dest" not in f:
                errors.append(f"[file] item {idx} missing 'dest'")
            if "extract" in f and not isinstance(f["extract"], bool):
                errors.append(f"[file] item {idx} 'extract' must be a boolean")
            if "strip_components" in f and not isinstance(f["strip_components"], int):
                errors.append(f"[file] item {idx} 'strip_components' must be an integer")
        return errors

    def pack(self, task_spec: dict[str, Any], ctx: PackContext) -> PluginResult:
        files = task_spec.get("files", [])
        if not files:
            return PluginResult(True, "No files to download", [], [])

        errors = []
        artifacts: list[str] = []
        slug = ctx.task_slug or "default"
        file_dir = ctx.bundle_dir / slug
        file_dir.mkdir(parents=True, exist_ok=True)

        for f in files:
            url = f["url"]
            filename = url.split("/")[-1]
            target_path = file_dir / filename

            if ctx.dry_run:
                continue

            try:
                if ctx.verbose:
                    print(f"[file] Downloading {url} -> {target_path}...")

                # Check cache first
                cache_dir = Path("~/.cache/syncit/file").expanduser()
                cache_dir.mkdir(parents=True, exist_ok=True)
                cache_path = cache_dir / filename

                if ctx.no_cache or not cache_path.exists():
                    if not (url.startswith("http://") or url.startswith("https://")):
                        raise ValueError(f"Invalid URL scheme (only http/https allowed): {url}")
                    urllib.request.urlretrieve(url, cache_path)

                shutil.copy2(cache_path, target_path)
                artifacts.append(f"{slug}/{filename}")
            except Exception as e:
                errors.append(f"[file] Failed to download {url}: {e}")

        return PluginResult(
            success=len(errors) == 0,
            message=f"Packed {len(files)} file(s)" if not errors else f"Failed to pack files: {errors}",
            artifacts=artifacts,
            errors=errors,
        )

    def apply(self, task_spec: dict[str, Any], ctx: ApplyContext) -> PluginResult:
        files = task_spec.get("files", [])
        if not files:
            return PluginResult(True, "No files to apply", [], [])

        errors = []
        artifacts: list[str] = []
        slug = ctx.task_slug or "default"
        file_dir = ctx.bundle_dir / slug

        for f in files:
            url = f["url"]
            filename = url.split("/")[-1]
            src_path = file_dir / filename
            dest_path = Path(f["dest"]).expanduser()
            import os
            if not os.path.abspath(dest_path).startswith("/srv/offline"):
                import sys
                print(f"[file] WARNING: Writing to arbitrary path {dest_path}", file=sys.stderr)

            extract_flag = f.get("extract", False)
            strip_components = f.get("strip_components", 0)

            if not src_path.exists():
                errors.append(f"[file] Source file missing: {src_path}")
                continue

            if ctx.dry_run:
                continue

            try:
                if extract_flag:
                    dest_path.mkdir(parents=True, exist_ok=True)
                    if filename.endswith(".zip"):
                        import zipfile
                        with zipfile.ZipFile(src_path, 'r') as zip_ref:
                            if strip_components > 0:
                                for member in zip_ref.infolist():
                                    parts = Path(member.filename).parts
                                    if len(parts) > strip_components:
                                        new_name = str(Path(*parts[strip_components:]))
                                        target_file = dest_path / new_name
                                        if member.is_dir():
                                            target_file.mkdir(parents=True, exist_ok=True)
                                        else:
                                            target_file.parent.mkdir(parents=True, exist_ok=True)
                                            with zip_ref.open(member) as source, open(target_file, "wb") as target:
                                                shutil.copyfileobj(source, target)
                            else:
                                zip_ref.extractall(dest_path)
                    elif filename.endswith(".tar.gz") or filename.endswith(".tgz") or filename.endswith(".tar"):
                        import tarfile
                        mode = 'r:gz' if filename.endswith('.gz') or filename.endswith('.tgz') else 'r'
                        with tarfile.open(src_path, mode) as tar:
                            if strip_components > 0:
                                members = []
                                for member in tar.getmembers():
                                    parts = Path(member.name).parts
                                    if len(parts) > strip_components:
                                        member.name = str(Path(*parts[strip_components:]))
                                        members.append(member)
                                tar.extractall(dest_path, members=members)
                            else:
                                tar.extractall(dest_path)
                    else:
                        errors.append(f"[file] Unsupported archive format for extraction: {filename}")
                        continue
                    artifacts.append(str(dest_path))
                else:
                    dest_path.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(src_path, dest_path)
                    if f.get("executable", False):
                        dest_path.chmod(0o755)
                    artifacts.append(str(dest_path))
            except Exception as e:
                errors.append(f"[file] Failed to apply {filename} to {dest_path}: {e}")

        return PluginResult(
            success=len(errors) == 0,
            message="Applied files",
            artifacts=artifacts,
            errors=errors,
        )

    def diff(self, old_spec: dict[str, Any] | None, new_spec: dict[str, Any]) -> DiffResult:
        old_list = (old_spec or {}).get("files", [])
        new_list = new_spec.get("files", [])

        old_urls = {f.get("url") for f in old_list if f.get("url")}
        new_urls = {f.get("url") for f in new_list if f.get("url")}

        added = list(new_urls - old_urls)
        removed = list(old_urls - new_urls)

        return DiffResult(
            plugin_name=self.name,
            added=added,
            removed=removed,
            updated=[],
            unchanged=[],
        )

    def render_apply_sh(self, task_spec: dict[str, Any], bundle_subdir: str) -> str:
        lines = ["echo '[file] Installing downloaded files...'"]
        files = task_spec.get("files", [])
        for f in files:
            url = f["url"]
            filename = url.split("/")[-1]
            dest = f["dest"]
            import os
            from pathlib import Path
            if not os.path.abspath(Path(dest).expanduser()).startswith("/srv/offline"):
                lines.append(f"echo '[file] WARNING: Writing to arbitrary path {dest}' >&2")
            exec_flag = f.get("executable", False)
            extract_flag = f.get("extract", False)
            strip_components = f.get("strip_components", 0)

            if extract_flag:
                lines.append(f"mkdir -p {dest}")
                if filename.endswith(".zip"):
                    if strip_components > 0:
                        lines.append(f"TMP_ZIP_DIR=$(mktemp -d)")
                        lines.append(f"unzip -q $BUNDLE_DIR/{bundle_subdir}/{filename} -d $TMP_ZIP_DIR")
                        lines.append(
                            f"find \"$TMP_ZIP_DIR\" -mindepth {strip_components} -maxdepth {strip_components} "
                            f"-exec cp -a {{}}/. \"{dest}/\" \\; 2>/dev/null || "
                            f"find \"$TMP_ZIP_DIR\" -mindepth {strip_components} -maxdepth {strip_components} "
                            f"-exec cp -a {{}} \"{dest}/\" \\;"
                        )
                        lines.append(f"rm -rf \"$TMP_ZIP_DIR\"")
                    else:
                        lines.append(f"unzip -q $BUNDLE_DIR/{bundle_subdir}/{filename} -d {dest}")
                elif filename.endswith(".tar.gz") or filename.endswith(".tgz"):
                    strip_arg = f" --strip-components={strip_components}" if strip_components > 0 else ""
                    lines.append(f"tar -xzf $BUNDLE_DIR/{bundle_subdir}/{filename} -C {dest}{strip_arg}")
                else:
                    strip_arg = f" --strip-components={strip_components}" if strip_components > 0 else ""
                    lines.append(f"tar -xf $BUNDLE_DIR/{bundle_subdir}/{filename} -C {dest}{strip_arg}")
            else:
                lines.append(f"mkdir -p $(dirname {dest})")
                lines.append(f"cp $BUNDLE_DIR/{bundle_subdir}/{filename} {dest}")
                if exec_flag:
                    lines.append(f"chmod +x {dest}")
        return "\n".join(lines) + "\n"


registry.register(FilePlugin())
