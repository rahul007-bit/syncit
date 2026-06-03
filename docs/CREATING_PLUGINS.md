# Creating a syncit Plugin

syncit uses a plugin-based architecture. Each plugin handles one artifact type
(apt, dnf, pip, oci_image, npm, cargo, go, file, etc.). This guide walks you
through creating a new plugin.

## Overview

A plugin is a Python class that inherits from `OfflinePlugin` and implements
five methods:

| Method | Phase | Description |
|--------|-------|-------------|
| `validate()` | pre-flight | Check the task spec for errors |
| `pack()` | online VM | Download artifacts into the bundle |
| `apply()` | offline VM | Install artifacts from the bundle |
| `diff()` | comparison | Show what changed between two bundles |
| `render_apply_sh()` | remote | Generate a bash script for zero-dep remote apply |

## Step-by-Step

### 1. Create the plugin file

Create `syncit/plugins/<name>.py`:

```python
"""<name> plugin — brief description."""

from __future__ import annotations

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


class <Name>Plugin(OfflinePlugin):
    name = "<name>"  # Must match the `plugin:` field in bundle.yaml

    def validate(self, task_spec: dict[str, Any]) -> list[str]:
        errors: list[str] = []
        # Check required fields, types, tool availability
        # Return empty list if valid
        return errors

    def pack(self, task_spec: dict[str, Any], ctx: PackContext) -> PluginResult:
        # ctx.bundle_dir / <name>/  — where artifacts go
        # ctx.dry_run — skip actual downloads if True
        # ctx.verbose — print progress
        # ctx.no_cache — force fresh downloads
        # ctx.manifest_dir — location of bundle.yaml (for relative paths)
        return PluginResult(
            success=True,
            message="Packed successfully",
            artifacts=[],  # paths relative to bundle_dir
            errors=[],
        )

    def apply(self, task_spec: dict[str, Any], ctx: ApplyContext) -> PluginResult:
        # ctx.bundle_dir / <name>/  — where artifacts are
        # ctx.state_file — path to state.json
        # ctx.dry_run — skip system changes if True
        # ctx.force — re-apply even if state says done
        return PluginResult(
            success=True,
            message="Applied successfully",
            artifacts=[],
            errors=[],
        )

    def diff(self, old_spec: dict[str, Any] | None, new_spec: dict[str, Any]) -> DiffResult:
        # old_spec=None means this task is brand new (all added)
        return DiffResult(
            plugin_name=self.name,
            added=[],
            removed=[],
            updated=[],
            unchanged=[],
        )

    def render_apply_sh(self, task_spec: dict[str, Any], bundle_subdir: str) -> str:
        # Generate bash that works with native OS tools (no Python on target)
        # $BUNDLE_DIR is the bundle root on the remote VM
        lines = [
            "echo '[<name>] Installing...'",
            # bash commands using $BUNDLE_DIR/<bundle_subdir>/
        ]
        return "\n".join(lines) + "\n"


# Register at module level so the plugin auto-registers on import
registry.register(<Name>Plugin())
```

### 2. Register the plugin in CLI commands

All plugins must be imported in the CLI command files so they register
themselves before commands run. Add your plugin to:

**`syncit/commands/validate.py`**
```python
import syncit.plugins.<name>  # noqa: F401
```

**`syncit/commands/pack.py`**
```python
import syncit.plugins.<name>  # noqa: F401
```

**`syncit/commands/apply.py`**
```python
import syncit.plugins.<name>  # noqa: F401
```

**`syncit/commands/diff.py`**
```python
import syncit.plugins.<name>  # noqa: F401
```

### 3. Add documentation

Add a section in `docs/DOCUMENTATION.md` under the Plugin Reference for your
plugin, documenting its task spec fields and bundle layout.

### 4. Bundle layout convention

```
bundle-<name>-<version>/
└── <plugin-name>/
    ├── ... (artifacts)
    └── ... (metadata)
```

For example, the `file` plugin produces:

```
bundle-<name>-<version>/file/
├── prometheus-2.45.0.linux-amd64.tar.gz
├── yq_linux_amd64
└── jq-linux64
```

## Full Plugin Example: `file`

See [`syncit/plugins/file.py`](../syncit/plugins/file.py) for a complete
reference implementation that handles:
- Basic file downloads with caching
- Archive extraction (tar.gz, zip) with strip_components
- Executable permission setting
- Dry-run and verbose support
- Zero-dep remote apply via bash

## API Reference

### `PackContext`

| Field | Type | Description |
|-------|------|-------------|
| `bundle_dir` | `Path` | Root directory of the bundle being built |
| `manifest_dir` | `Path` | Directory containing `bundle.yaml` |
| `dry_run` | `bool` | If True, only simulate |
| `verbose` | `bool` | If True, print progress details |
| `no_cache` | `bool` | If True, force fresh downloads |

### `ApplyContext`

| Field | Type | Description |
|-------|------|-------------|
| `bundle_dir` | `Path` | Root directory of the bundle to apply |
| `state_file` | `Path` | Path to state.json on the offline VM |
| `dry_run` | `bool` | If True, only simulate |
| `verbose` | `bool` | If True, print progress details |
| `force` | `bool` | If True, re-apply even if state says done |

### `PluginResult`

| Field | Type | Description |
|-------|------|-------------|
| `success` | `bool` | Whether the operation succeeded |
| `message` | `str` | Human-readable result message |
| `artifacts` | `list[str]` | Paths written to or applied from the bundle |
| `errors` | `list[str]` | Error messages (empty if success) |

### `DiffResult`

| Field | Type | Description |
|-------|------|-------------|
| `plugin_name` | `str` | Plugin identifier |
| `added` | `list[str]` | Items present in new but not old |
| `removed` | `list[str]` | Items present in old but not new |
| `updated` | `list[str]` | Items that changed version/checksum |
| `unchanged` | `list[str]` | Items identical between old and new |

## Best Practices

1. **Cache aggressively** — Use `~/.cache/syncit/<plugin>/` for downloads so
   repeated `pack` operations are fast. Respect `ctx.no_cache`.

2. **Respect dry-run** — Never modify the system or download files when
   `ctx.dry_run` is True. Return what would happen in the message.

3. **Idempotent apply** — Skip already-applied artifacts on the offline VM.
   Check if packages are installed before installing.

4. **Consistent error handling** — Collect all errors (don't early-exit on
   the first one) so the user sees everything wrong at once.

5. **Zero-dep remote scripts** — `render_apply_sh()` should only use native
   OS tools (bash, cp, tar, apt-get, dnf, etc.). The remote VM does not have
   Python or syncit installed.

6. **Bundle isolation** — All artifacts go under `ctx.bundle_dir / <name>/`.
   Never write outside the bundle directory during `pack()`.