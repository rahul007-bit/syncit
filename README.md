# syncit

**Air-gap bundle orchestrator for Linux environments.**

`syncit` lets you download OS packages, Python wheels, container images, Node modules,
Rust crates, Go modules, RPM packages, and arbitrary files on an internet-connected
machine, then apply them all on an air-gapped VM — no proxy, no internet required.

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
apiVersion: syncit/v1
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

For third-party packages (Kubernetes, Docker, Grafana), declare repos inline:

```yaml
- name: Kubernetes components
  plugin: apt
  repos:
    - name: kubernetes
      url: "deb [signed-by=/etc/apt/keyrings/kubernetes.gpg] https://pkgs.k8s.io/core:/stable:/v1.31/deb/ /"
      gpg_key: https://pkgs.k8s.io/core:/stable:/v1.31/deb/Release.key
  packages: [kubeadm, kubelet, kubectl]
```

### 3. Validate

```bash
syncit validate bundle.yaml
```

### 4. Pack (on online VM)

```bash
syncit pack bundle.yaml --output ./bundles/
```

### 5. Transfer

Copy the `bundles/bundle-my-env-1.0.0/` directory to the offline VM via USB, SCP, or
archive it first:

```bash
syncit pack bundle.yaml --output ./bundles/ --format tar.gz
```

### 6. Apply (on offline VM)

```bash
syncit apply ./bundles/bundle-my-env-1.0.0/
# Or remotely without syncit on the target:
syncit apply --bundle bundle.tar.gz -i inv.yaml -t my-server
```

### 7. Diff two bundles

```bash
syncit diff ./bundles/bundle-my-env-1.0.0/ ./bundles/bundle-my-env-2.0.0/
```

---

## CLI Reference

```
syncit --help

Commands:
  validate      Validate a bundle.yaml manifest file.
  pack          Download and bundle all dependencies (run on online VM).
  apply         Apply a bundle onto this machine (run on offline VM).
  diff          Compare two bundle versions and show what changed.
  up            Pack and remotely apply in one command.
  transfer      SCP bundle to remote hosts.
  apply-remote  Zero-dependency remote apply via SSH.
  exec          Run shell commands on remote hosts via SSH.
```

| Command | Description |
|---------|-------------|
| `validate <manifest>` | Validate manifest + plugin specs |
| `pack <manifest> -o <dir>` | Download artifacts to bundle (online) |
| `apply <bundle>` | Install from bundle (offline) |
| `diff <v1> <v2>` | Show what changed between bundles |
| `up <manifest> -i inv.yaml -t <host>` | Pack + apply remote in one step |
| `exec -i inv.yaml -t <host> -- <cmd>` | Run commands on remote hosts |
| `apply --bundle <tar> -i inv.yaml -t <host>` | Zero-dep remote apply |

Flags: `--dry-run`, `--verbose`, `--force`, `--only <plugins>`, `--format tar.gz|zip|dir`.

---

## Supported Plugins

| Plugin | Description |
|--------|-------------|
| `apt` | Ubuntu/Debian `.deb` packages with dependency resolution |
| `dnf` | RHEL/Rocky/Alma `.rpm` packages with dependency resolution |
| `pip` | Python wheels via requirements.txt |
| `oci_image` | Docker/OCI container images via skopeo |
| `file` | Arbitrary files, tarballs, and binaries from URLs |
| `npm` | Node.js node_modules (vendored) |
| `cargo` | Rust vendored dependencies |
| `go` | Go module cache |

All plugins support inline `repos` for third-party packages — no manual repo
configuration needed on the online machine.

---

## Bundle Directory Format

```
bundle-my-env-1.0.0/
├── bundle.meta.json         # Metadata + targets + task status
├── bundle.yaml              # Copy of manifest
├── apt/                     # .deb packages (if apt task)
│   ├── debs/               # Downloaded .deb files + Packages index
│   ├── sources.list         # Local apt source fragment
│   ├── keys/                # GPG keys for third-party repos
│   └── repos.json           # Repo metadata for audit/diff
├── dnf/                     # .rpm packages (if dnf task)
│   ├── rpms/               # Downloaded .rpm files + repodata
│   ├── keys/                # GPG keys for third-party repos
│   └── repos.json           # Repo metadata for audit/diff
├── pip/                     # Python wheels (if pip task)
│   ├── wheels/
│   └── requirements.txt
├── images/                  # OCI images (if oci_image task)
│   ├── manifest.json
│   └── *.tar
├── file/                    # Downloaded files (if file task)
├── npm/                     # Vendored node_modules (if npm task)
├── cargo/                   # Vendored crates (if cargo task)
└── go/                      # Go module cache (if go task)
```

---

## Docs

For full documentation, see [`docs/DOCUMENTATION.md`](docs/DOCUMENTATION.md):

- [Manifest reference](docs/DOCUMENTATION.md#3-the-manifest-bundleyaml)
- [Plugin reference](docs/DOCUMENTATION.md#5-plugin-reference) (apt, dnf, pip, oci_image, npm, cargo, go, file)
- [Repository requirements](docs/DOCUMENTATION.md#repository-requirements-for-third-party-packages)
- [Roles system](docs/DOCUMENTATION.md#6-roles-system)
- [Inventory & remote hosts](docs/DOCUMENTATION.md#7-inventory--remote-hosts)
- [Bundle structure](docs/DOCUMENTATION.md#8-bundle-structure)
- [State & idempotency](docs/DOCUMENTATION.md#9-state--idempotency)
- [Zero-dependency remote apply](docs/DOCUMENTATION.md#10-zero-dependency-remote-apply)
- [End-to-end examples](docs/DOCUMENTATION.md#11-end-to-end-workflow-examples)

For creating new plugins, see [`docs/CREATING_PLUGINS.md`](docs/CREATING_PLUGINS.md).

---

## Development

```bash
# Install with dev dependencies
pip install -e ".[dev]"

# Run tests
pytest

# Format
ruff format syncit/ tests/

# Type check
mypy syncit/ --ignore-missing-imports
```

### Requirements per plugin

| Plugin | Online VM (pack) | Offline VM (apply) |
|--------|------------------|-------------------|
| `apt` | `apt-get`, `apt-cache`, `dpkg-scanpackages` | `apt-get` |
| `dnf` | `dnf`, `createrepo_c`, `dnf-plugins-core` | `dnf`, `createrepo_c` |
| `pip` | `pip3` | `pip3` |
| `oci_image` | `skopeo` | `docker`/`podman`/`ctr` |
| `file` | None (Python urllib) | None |
| `npm` | `node`, `npm` | None |
| `cargo` | `cargo` | None |
| `go` | `go` | None |