# offlinectl

**Air-gap bundle orchestrator for Linux environments.**

`offlinectl` lets you download all OS packages, Python wheels, and container images on an internet-connected machine, then apply them on an air-gapped VM — no proxy, no internet required.

---

## Quickstart

### 1. Install

```bash
pip install .
# or in development mode:
pip install -e ".[dev]"
```

### 2. Write a manifest

```yaml
# bundle.yaml
apiVersion: offlinectl/v1
kind: Bundle
metadata:
  name: my-env
  version: "1.0.0"
spec:
  targets:
    distro: ubuntu
    codename: noble
    arch: amd64
  tasks:
    - name: Install packages
      plugin: apt
      packages: [git, curl, python3.12]

    - name: Install Python deps
      plugin: pip
      python_version: "3.12"
      requirements: ./requirements.txt

    - name: Pull images
      plugin: oci_image
      images:
        - source: docker.io/library/redis:7-alpine
```

### 3. Validate

```bash
offlinectl validate bundle.yaml
```

### 4. Pack (on online VM)

```bash
offlinectl pack bundle.yaml --output ./bundles/
# Dry-run first:
offlinectl pack bundle.yaml --output ./bundles/ --dry-run
```

### 5. Transfer the bundle

Copy the `bundles/bundle-my-env-1.0.0/` directory to the offline VM via USB, SCP, or any other means.

### 6. Apply (on offline VM)

```bash
offlinectl apply ./bundles/bundle-my-env-1.0.0/
# Dry-run first:
offlinectl apply ./bundles/bundle-my-env-1.0.0/ --dry-run
# Force re-apply even if already applied:
offlinectl apply ./bundles/bundle-my-env-1.0.0/ --force
```

### 7. Diff two bundles

```bash
offlinectl diff ./bundles/bundle-my-env-1.0.0/ ./bundles/bundle-my-env-2.0.0/
```

---

## CLI Reference

```
offlinectl --help

Commands:
  validate   Validate a bundle.yaml manifest file.
  pack       Download and bundle all dependencies (run on online VM).
  apply      Apply a bundle onto this machine (run on offline VM).
  diff       Compare two bundle versions and show what changed.
```

### `offlinectl validate <manifest>`

| Flag | Description |
|------|-------------|
| (positional) | Path to `bundle.yaml` |

### `offlinectl pack <manifest>`

| Flag | Default | Description |
|------|---------|-------------|
| `--output` / `-o` | `.` | Output directory |
| `--dry-run` | false | Print what would happen |
| `--only` | all | Comma-separated plugin names to run |
| `--verbose` / `-v` | false | Verbose output |

### `offlinectl apply <bundle_dir>`

| Flag | Default | Description |
|------|---------|-------------|
| `--dry-run` | false | Show what would be applied |
| `--force` | false | Re-apply even if state says done |
| `--only` | all | Comma-separated plugin names |
| `--state-file` | `/opt/offlinectl/state.json` | State file path |
| `--continue-on-error` | false | Don't stop on plugin failure |

### `offlinectl diff <bundle_v1> <bundle_v2>`

Compares two bundle directories; prints `+` added, `-` removed, `~` updated per plugin.

---

## Supported Plugins

| Plugin | Status | Description |
|--------|--------|-------------|
| `apt` | ✅ Phase 1 | Ubuntu/Debian `.deb` packages |
| `pip` | ✅ Phase 1 | Python wheels via requirements.txt |
| `oci_image` | ✅ Phase 1 | Docker/OCI images via skopeo |
| `npm` | 🔲 Phase 2 | Node.js packages |
| `cargo` | 🔲 Phase 2 | Rust crates |
| `go` | 🔲 Phase 2 | Go modules |

---

## Bundle Directory Format

```
bundle-my-env-1.0.0/
├── bundle.meta.json      # Bundle metadata + targets
├── bundle.yaml           # Copy of manifest
├── apt/
│   ├── debs/             # Downloaded .deb files
│   ├── Packages          # dpkg-scanpackages index
│   └── sources.list      # Local apt source fragment
├── pip/
│   ├── wheels/           # Downloaded .whl files
│   └── requirements.txt  # Requirements snapshot
└── images/
    ├── manifest.json     # Image list + digests
    ├── alpine_latest.tar
    └── ...
```

---

## Development

```bash
# Install with dev dependencies
pip install -e ".[dev]"

# Run tests
pytest

# Lint
ruff check offlinectl/ tests/
```

### Requirements

- **Online VM** (pack): `apt-get`, `pip`, `skopeo`, `dpkg-scanpackages`
- **Offline VM** (apply): `apt-get`, `pip`, one of `docker`/`podman`/`ctr`
