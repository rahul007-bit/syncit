# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

`syncit` is an air-gap bundle orchestrator for Linux environments. It lets you download OS packages, Python wheels, and container images on an internet-connected machine, then apply them on an air-gapped VM via a portable bundle directory.

The CLI binary is named `syncit` but the Python package directory remains `syncit/` — internal imports use `from syncit.xxx import yyy`.

**Python version**: requires >=3.11 (uses `datetime.UTC`, `from __future__ import annotations`)
**Build system**: hatchling — configured to find packages under `syncit/` via `tool.hatch.build.targets.wheel.packages`

## Development Commands

### Linting & Type Checking
```bash
uv run ruff format --check syncit/ tests/   # format check
uv run ruff format syncit/ tests/            # auto-format
uv run mypy syncit/ --ignore-missing-imports
```

### Testing
```bash
uv run pytest -q
uv run pytest tests/test_exec_cmd.py            # specific file
```

### CLI Usage
Run via `uv run syncit` or install the package and use `syncit` directly:
```bash
syncit validate <manifest>              # Validate bundle.yaml and plugin specs
syncit pack <manifest> [--output DIR]    # Download dependencies (online VM)
syncit apply <bundle_dir> [--dry-run]   # Install bundle artifacts (offline VM)
syncit diff <bundle_v1> <bundle_v2>     # Compare two bundles
syncit transfer <bundle> -i inv.yaml -t <target>   # SCP bundle to remote hosts
syncit apply-remote --bundle <archive> -i inv.yaml -t <target>  # Zero-dep remote apply via SSH
syncit exec -i inv.yaml -t <target> -- <command>  # Run shell command on remote hosts
```

### Key Files
- `syncit/main.py` — Typer app registration
- `syncit/commands/` — CLI command implementations
- `syncit/plugins/` — Plugin registry and implementations (apt, pip, oci_image, npm, cargo, go)
- `syncit/manifest/schema.py` — Pydantic model for `bundle.yaml`
- `syncit/inventory/` — Inventory system for remote host management
- `syncit/bundle/` — Bundle metadata, state tracking, and archive handling

## Architecture

### Plugin System
`syncit` uses a plugin-based architecture. Each plugin inherits from `OfflinePlugin` (`plugins/base.py`):
- `validate()` — check task specification
- `pack()` — download artifacts to bundle directory (online)
- `apply()` — install artifacts on target host (offline)
- `diff()` — compare two specifications

Plugins are registered in `plugins/registry.py` via the `@registry.register` decorator and auto-imported in command files to register the plugin.

### Inventory & Remote Hosts
- `inventory/schema.py` — `Host` (host, user, ssh_key, bundle_dest, state_file) and `Inventory` (hosts dict + groups dict)
- `inventory/loader.py` — `load_inventory()` validates and parses YAML; `resolve_targets()` resolves a target string (host name or group) into a list of `(host_id, Host)` tuples
- `commands/apply_remote.py` — uses SSH/SCP to run the apply script on remote hosts; embeds a generated bash script rather than requiring syncit on the target
- `commands/exec_cmd.py` — runs arbitrary commands over SSH with parallel execution via ThreadPoolExecutor

### Bundle Structure
```
bundle-name/
├── bundle.yaml          # Original manifest
├── bundle.meta.json     # Metadata (version, created_at, syncit_version, targets, tasks)
├── apt/                 # .deb files, Packages index, sources.list
├── pip/                 # wheels/, requirements.txt
├── images/              # OCI image tarballs + manifest.json
└── ...
```

State is tracked in `/opt/syncit/state.json` (configurable) to ensure idempotent/repeated application.

## Important Notes

- The manifest `apiVersion` is `syncit/v1` — changing this would break manifest validation
- Internal imports use `from syncit.xxx` — do NOT rename the package directory
- Some `datetime.UTC` usages may cause import errors on Python < 3.11
- Tests use `unittest.mock.patch` extensively; when patching `ThreadPoolExecutor` or `as_completed` inside `exec_cmd.py`, patch at `syncit.commands.exec_cmd.ThreadPoolExecutor`
- When testing commands via `CliRunner`, subprocess calls inside command modules may need patching at the module-level path (e.g., `syncit.commands.exec_cmd.subprocess`)
