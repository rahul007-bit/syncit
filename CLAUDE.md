# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Development Commands

### Testing
Run all tests:
```bash
export PYTHONPATH=$PYTHONPATH:$(pwd)
python3 -m pytest tests/
```

Run a specific test file:
```bash
export PYTHONPATH=$PYTHONPATH:$(pwd)
python3 -m pytest tests/test_plugin_pip.py
```

### CLI Usage
The tool is implemented using Typer. Run via `python3 -m offlinectl.main` or install via `pip install .` and use `syncit`.

Common commands:
- `syncit validate <manifest>`: Validate `bundle.yaml` and plugin specs.
- `syncit pack <manifest> [--output DIR] [--dry-run]`: Resolve and download dependencies.
- `syncit apply <bundle_dir> [--dry-run] [--force]`: Install bundle artifacts on air-gapped host.
- `syncit diff <bundle_v1> <bundle_v2>`: Compare two bundles.

## Architecture

`syncit` follows a plugin-based orchestration architecture to handle different package ecosystems (apt, pip, oci_image).

### Core Flow
1. **Manifest**: `manifest/schema.py` defines the Pydantic model for `bundle.yaml`.
2. **Registry**: `plugins/registry.py` maintains a mapping of plugin names to `OfflinePlugin` implementations.
3. **Plugin Interface**: All plugins must inherit from `OfflinePlugin` in `plugins/base.py`, implementing:
    - `validate()`: Checks task specification.
    - `pack()`: Downloads artifacts to the bundle directory (Online VM).
    - `apply()`: Installs artifacts and configures the system (Offline VM).
    - `diff()`: Computes differences between two specifications.
4. **Commands**: Logic for CLI orchestration is split into `commands/pack.py`, `commands/apply.py`, etc., which manage the lifecycle of the bundle and call the respective plugins.
5. **State Tracking**: `bundle/state.py` manages a `state.json` file on the target host to ensure idempotence and track applied versions.

### Bundle Structure
Bundles are plain directories containing:
- `bundle.yaml`: The original manifest.
- `bundle.meta.json`: Metadata about the bundle creation and targets.
- Plugin-specific directories (e.g., `apt/`, `pip/`, `images/`) containing the downloaded artifacts.
