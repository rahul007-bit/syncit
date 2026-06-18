# syncit — Air-Gap Bundle Orchestrator

**syncit** lets you download OS packages, Python wheels, container images, Node modules,
Rust crates, Go modules, RPM packages, and arbitrary files on an internet-connected machine,
then apply them all on an air-gapped (offline) VM — no proxy, no internet required.

---

## Table of Contents

1. [Overview & Architecture](#1-overview--architecture)
2. [Installation & Prerequisites](#2-installation--prerequisites)
   - [Repository Requirements for Third-Party Packages](#repository-requirements-for-third-party-packages)
3. [The Manifest (bundle.yaml)](#3-the-manifest-bundleyaml)
4. [CLI Commands](#4-cli-commands)
5. [Plugin Reference](#5-plugin-reference)
   - [apt — Debian/Ubuntu packages](#51-apt--debianubuntu-packages)
   - [dnf — RPM packages (RHEL/Rocky/Alma/CentOS)](#52-dnf--rpm-packages)
   - [pip — Python wheels](#53-pip--python-wheels)
   - [oci_image — Docker/OCI container images](#54-oci_image--dockeroci-container-images)
   - [npm — Node.js node_modules](#55-npm--nodejs-node_modules)
   - [cargo — Rust vendored dependencies](#56-cargo--rust-vendored-dependencies)
   - [go — Go module cache](#57-go--go-module-cache)
   - [file — Arbitrary files and archives](#58-file--arbitrary-files-and-archives)
6. [Roles System](#6-roles-system)
7. [Inventory & Remote Hosts](#7-inventory--remote-hosts)
8. [Bundle Structure](#8-bundle-structure)
9. [State & Idempotency](#9-state--idempotency)
10. [Zero-Dependency Remote Apply](#10-zero-dependency-remote-apply)
11. [End-to-End Workflow Examples](#11-end-to-end-workflow-examples)

---

## 1. Overview & Architecture

syncit follows a **pack → transfer → apply** workflow:

```
┌─────────────────────────┐        ┌──────────────────────────┐
│    ONLINE MACHINE       │        │     OFFLINE VM           │
│  (has internet access)  │        │   (air-gapped / no net)  │
│                         │        │                          │
│  1. Write bundle.yaml   │        │                          │
│  2. syncit validate     │──USB──►│  6. syncit apply         │
│  3. syncit pack         │  SCP   │     (or syncit transfer  │
│                         │        │      + manual copy)      │
└─────────────────────────┘        └──────────────────────────┘
```

### Key Concepts

- **Manifest (`bundle.yaml`)** — Declares what to pack (packages, images, files) and target platform info
- **Pack** — Download all artifacts from the internet into a portable bundle directory
- **Bundle** — Self-contained directory (or `.tar.gz`/`.zip` archive) with all downloaded artifacts
- **Apply** — Install everything on the offline VM using the local bundle artifacts
- **Plugin** — Each artifact type (apt, pip, oci_image, etc.) is handled by a self-contained plugin
- **State file** — Tracks what was applied (and when) for idempotent re-runs
- **Role** — Reusable group of tasks defined in a separate `role.yaml` file

---

## 2. Installation & Prerequisites

### Installing syncit

```bash
# From the project root
pip install .

# Or with dev dependencies for development
pip install -e ".[dev]"
```

### Online VM Requirements (for `pack`)

| Plugin     | Required Tools                              |
|------------|---------------------------------------------|
| **apt**    | `apt-get`, `apt-cache`, `dpkg-scanpackages` (install: `sudo apt install dpkg-dev`) |
| **dnf**    | `dnf`, `createrepo_c` (install: `sudo dnf install createrepo_c`) |
| **pip**    | `pip3` or `pip`, `python3` |
| **oci_image** | `docker`, `podman`, or `skopeo` |
| **npm**    | `node`, `npm` |
| **cargo**  | `cargo` (Rust toolchain) |
| **go**     | `go` (Go toolchain) |
| **file**   | None (uses Python stdlib `urllib`) |

### Offline VM Requirements (for `apply`)

| Plugin     | Required Tools                              |
|------------|---------------------------------------------|
| **apt**    | `apt-get`, `dpkg-scanpackages` (`dpkg-dev`) |
| **dnf**    | `dnf`, `createrepo_c` |
| **pip**    | `pip3` or `pip` |
| **oci_image** | One of: `docker`, `podman`, or `ctr` (containerd) |
| **npm**    | None (just filesystem operations) |
| **cargo**  | None (just filesystem operations) |
| **go**     | None (just filesystem + environment setup) |
| **file**   | None (just filesystem operations) |

### Repository Requirements for Third-Party Packages

syncit **does not** add or configure upstream package repositories during `pack`. It relies on whatever
repos are already configured on the online machine. This matters when you need packages from
third-party sources that aren't in the default OS repos.

| Scenario | Default OS repos | Need to add manually |
|----------|------------------|----------------------|
| Ubuntu/Debian base packages (`git`, `curl`, `nginx`) | ✅ Included | ❌ Nothing extra |
| RHEL/Rocky/Alma base packages (`git`, `nginx`, `python3`) | ✅ Included | ❌ Nothing extra |
| **Kubernetes** (`kubeadm`, `kubelet`, `kubectl`) | ❌ Not in default repos | ⚠️ Add Kubernetes repo on the online machine first |
| **Docker** (`docker-ce`, `docker-ce-cli`) | ❌ Not in default repos | ⚠️ Add Docker repo on the online machine first |
| **Grafana**, **Prometheus** (via apt/dnf) | ❌ Not in default repos | ⚠️ Add their repo on the online machine first |
| **Hashicorp** tools (`terraform`, `vault`) | ❌ Not in default repos | ⚠️ Add Hashicorp repo on the online machine first |

**Important:** These repos are only needed on the **online** (internet-connected) machine where
`syncit pack` runs. The offline VM gets the downloaded packages from the bundle — it never needs
access to these remote repos.

#### Example: Adding the Kubernetes repo for apt (Ubuntu/Debian)

```bash
# On the online machine only — before running syncit pack
sudo apt-get update
sudo apt-get install -y apt-transport-https ca-certificates curl gpg

# Add the Kubernetes apt key
curl -fsSL https://pkgs.k8s.io/core:/stable:/v1.31/deb/Release.key \
  | sudo gpg --dearmor -o /etc/apt/keyrings/kubernetes-apt-keyring.gpg

# Add the Kubernetes apt repo
echo 'deb [signed-by=/etc/apt/keyrings/kubernetes-apt-keyring.gpg] https://pkgs.k8s.io/core:/stable:/v1.31/deb/ /' \
  | sudo tee /etc/apt/sources.list.d/kubernetes.list

sudo apt-get update  # Now apt knows about k8s packages
```

Then in your manifest:
```yaml
tasks:
  - name: Kubernetes components
    plugin: apt
    packages: [kubeadm, kubelet, kubectl]
```

#### Example: Adding the Kubernetes repo for dnf (RHEL/Rocky/Alma)

```bash
# On the online machine only — before running syncit pack
cat <<EOF | sudo tee /etc/yum.repos.d/kubernetes.repo
[kubernetes]
name=Kubernetes
baseurl=https://pkgs.k8s.io/core:/stable:/v1.31/rpm/
enabled=1
gpgcheck=1
gpgkey=https://pkgs.k8s.io/core:/stable:/v1.31/rpm/repodata/repomd.xml.key
EOF

sudo dnf makecache  # Now dnf knows about k8s packages
```

> **Note:** syncit now supports declaring these repos directly in the manifest (see
> [apt `repos` field](#pack-phase--manifest-task-spec-1) and
> [dnf `repos` field](#pack-phase--manifest-task-spec-2)). No manual setup needed on the
> online machine — `syncit pack` handles it automatically.

### Jumphost Requirements (for remote commands)

| Command                  | Required Tools            |
|--------------------------|---------------------------|
| `syncit exec`            | `ssh`                     |
| `syncit apply` (remote)  | `ssh`, `scp`              |
| `syncit transfer`        | `ssh`, `scp`              |

---

## 3. The Manifest (bundle.yaml)

The manifest is a YAML file that declares everything syncit should pack and apply.

### Minimal Example

```yaml
apiVersion: syncit/v1
kind: Bundle
metadata:
  name: my-env
  version: "1.0.0"
  description: Essential tools for air-gapped deployment
spec:
  targets:
    distro: ubuntu
    codename: noble
    arch: amd64
  tasks:
    - name: Install system packages
      plugin: apt
      packages: [git, curl, python3.12]

    - name: Install Python deps
      plugin: pip
      python_version: "3.12"
      requirements: ./requirements.txt

    - name: Pull container images
      plugin: oci_image
      images:
        - source: docker.io/library/redis:7-alpine
        - source: docker.io/library/postgres:16
```

### Schema Reference

| Field                        | Required | Type           | Description                                |
|------------------------------|----------|----------------|--------------------------------------------|
| `apiVersion`                 | Yes      | string         | Must be `syncit/v1`                        |
| `kind`                       | Yes      | string         | Must be `Bundle`                           |
| `metadata.name`              | Yes      | string         | Bundle name (used in directory naming)     |
| `metadata.version`           | Yes      | string         | Bundle version (used in directory naming)  |
| `metadata.description`       | No       | string         | Human-readable description                 |
| `metadata.author`            | No       | string         | Author info                                |
| `spec.targets.distro`        | Yes      | string         | Target OS distro (e.g., `ubuntu`, `rhel`)  |
| `spec.targets.codename`      | Yes      | string         | Distro codename (e.g., `noble`, `jammy`)   |
| `spec.targets.arch`          | Yes      | string         | Architecture (e.g., `amd64`, `arm64`)      |
| `spec.roles`                 | No       | list           | Role references (see [Roles](#6-roles-system)) |
| `spec.tasks`                 | Yes      | list           | Plugin tasks (at least one required)       |

Each task entry requires:

| Field    | Required | Description                                    |
|----------|----------|------------------------------------------------|
| `name`   | Yes      | Unique task name (used in state tracking)      |
| `plugin` | Yes      | Plugin identifier: `apt`, `pip`, `oci_image`, `npm`, `cargo`, `go`, `file`, `dnf` |
| ...      | Varies   | Plugin-specific fields documented below         |

---

## 4. CLI Commands

### `syncit validate <manifest>`

Validates the manifest YAML schema and all plugin task specifications.

```bash
syncit validate bundle.yaml
```

- Checks manifest structure (apiVersion, kind, metadata)
- Validates each task against its plugin (required fields, tool availability)
- Prints per-task status: ✓ OK or ✗ error details

### `syncit pack <manifest>`

Downloads all dependencies and writes them into a self-contained bundle directory.
Run this on the internet-connected machine.

```bash
syncit pack bundle.yaml --output ./bundles/
```

| Flag                  | Default  | Description                                      |
|-----------------------|----------|--------------------------------------------------|
| `--output` / `-o`     | `.`      | Output directory for the bundle                  |
| `--dry-run`           | false    | Print what would be done, don't execute          |
| `--only`              | all      | Comma-separated plugin names to run              |
| `--format`            | `dir`    | Output format: `dir`, `tar.gz`, `zip`            |
| `--verbose` / `-v`    | false    | Verbose output with progress details             |
| `--no-cache`          | false    | Force re-download, ignore local cache            |

Output bundle directory: `bundle-<name>-<version>/`

### `syncit apply <bundle_dir>`

Installs all artifacts on the target machine from a local bundle directory.
Run this on the air-gapped machine.

```bash
syncit apply ./bundles/bundle-my-env-1.0.0/
```

| Flag                       | Default               | Description                                      |
|----------------------------|-----------------------|--------------------------------------------------|
| `--dry-run`                | false                 | Show what would be applied, don't execute        |
| `--force`                  | false                 | Re-apply even if state says already done         |
| `--only`                   | all                   | Comma-separated plugin names to run              |
| `--state-file`             | `/opt/syncit/state.json` | State file path                               |
| `--continue-on-error`      | false                 | Don't stop on plugin failure                     |

### `syncit apply --bundle <archive> --inventory <inv> --target <host>`

Zero-dependency remote apply. See [Section 10](#10-zero-dependency-remote-apply).

```bash
syncit apply --bundle ./bundle-my-env-1.0.0.tar.gz -i inv.yaml -t my-server
```

| Flag                       | Description                                           |
|----------------------------|-------------------------------------------------------|
| `--bundle` / `-b`          | Path to local bundle archive (tar.gz or zip)          |
| `--inventory` / `-i`       | Path to inventory YAML file                           |
| `--target` / `-t`          | Target host or group from inventory                   |
| `--print-script`           | Print generated apply.sh script and exit              |

### `syncit up`

Pack a bundle and immediately apply it remotely in one command.

```bash
syncit up bundle.yaml -i inv.yaml -t my-server
```

| Flag                  | Default  | Description                                      |
|-----------------------|----------|--------------------------------------------------|
| `-i` / `--inventory`  | Required | Path to inventory YAML file                      |
| `-t` / `--target`     | —        | Target host from inventory                       |
| `-g` / `--group`      | —        | Target group from inventory                      |
| `--all`               | false    | Apply to all hosts in the inventory              |
| `--format`            | `tar.gz` | Archive format for packing                       |
| `--verbose` / `-v`    | false    | Verbose output                                   |
| `--no-cache`          | false    | Force re-download                                |

### `syncit diff <bundle_v1> <bundle_v2>`

Compare two bundle directories (or archives) and show what changed per plugin.

```bash
syncit diff ./bundle-my-env-1.0.0/ ./bundle-my-env-2.0.0/
```

Output format per plugin:
- `+ item` — Added in v2
- `- item` — Removed in v2 (was in v1)
- `~ item` — Updated / changed
- `(no changes)` — Identical

### `syncit exec`

Run arbitrary shell commands on remote hosts via SSH in parallel.

```bash
syncit exec -i inv.yaml -t my-server -- ls -la /opt
syncit exec -i inv.yaml --all -- df -h
syncit exec -i inv.yaml -g webservers -- sudo systemctl status nginx
```

| Flag                  | Default | Description                                      |
|-----------------------|---------|--------------------------------------------------|
| `-i` / `--inventory`  | Required | Path to inventory YAML file                     |
| `-t` / `--target`     | —       | Target host from inventory                       |
| `-g` / `--group`      | —       | Target group from inventory                      |
| `--all`               | false   | Execute on all hosts in inventory                |
| `--sudo`              | false   | Run command with sudo on remote hosts            |
| `--timeout`           | 30      | SSH timeout in seconds                           |

Use `--` before the command to separate options from the command itself:

```bash
syncit exec -i inv.yaml -t db-server -- sudo systemctl restart postgresql
```

---

## 5. Plugin Reference

### 5.1 apt — Debian/Ubuntu packages

Downloads `.deb` packages with recursive dependency resolution, creates a local apt repository.

**Pack phase:** Resolves all dependencies via `apt-cache depends --recurse`, downloads each
`.deb` with `apt-get download`, generates `Packages` index via `dpkg-scanpackages`, and writes
a `sources.list` fragment for local use. Caches downloads at `~/.cache/syncit/apt/`.

> **Note:** syncit supports adding upstream repos automatically during `pack`
> via the `repos` field (see below). No manual repo setup needed on the online machine.

**Apply phase:** Copies `.deb` files to `/srv/offline/apt/debs/`, writes an apt source entry at
`/etc/apt/sources.list.d/offline.list`, runs `apt-get update`, then installs only packages not
already installed (idempotent). Warns if the bundle was packed on a different distro codename.

#### Manifest Task Spec

```yaml
- name: Install system tools
  plugin: apt
  packages:
    - git
    - curl
    - python3.12
    - build-essential
    - libssl-dev
```

Optionally, declare upstream repositories for third-party packages:

```yaml
- name: Kubernetes components
  plugin: apt
  repos:
    - name: kubernetes
      url: "deb [signed-by=/etc/apt/keyrings/kubernetes.gpg] https://pkgs.k8s.io/core:/stable:/v1.31/deb/ /"
      gpg_key: https://pkgs.k8s.io/core:/stable:/v1.31/deb/Release.key
  packages:
    - kubeadm
    - kubelet
    - kubectl
```

| Field                  | Required | Description                                      |
|------------------------|----------|--------------------------------------------------|
| `repos`                | No       | List of upstream repository definitions          |
| `repos[].name`         | Yes      | Unique short name for the repo                   |
| `repos[].url`          | Yes      | Full apt sources line (e.g., `deb [signed-by=...] https://... /`) |
| `repos[].gpg_key`      | No       | URL to download the repository GPG key (stored in bundle for audit) |

During `pack`, repos are injected via `-o Dir::Etc::SourceParts=<temp>` — no system files
are modified. GPG keys are stored in the bundle (`apt/keys/`) for audit and diff.
The offline `apply` phase uses the local `file://` repo only (upstream repos are irrelevant
on the air-gapped VM).

#### Example: Pack & Apply

```bash
# On online machine
cat > bundle.yaml << 'EOF'
apiVersion: syncit/v1
kind: Bundle
metadata:
  name: dev-tools
  version: "1.0.0"
spec:
  targets:
    distro: ubuntu
    codename: noble
    arch: amd64
  tasks:
    - name: System packages
      plugin: apt
      packages: [git, curl, vim, htop, tmux, python3.12, python3.12-venv]
EOF

# Validate
syncit validate bundle.yaml

# Pack
syncit pack bundle.yaml --output ./bundles/

# Copy bundle to offline VM via USB/SCP

# On offline machine
syncit apply ./bundles/bundle-dev-tools-1.0.0/
```

#### What gets bundled

```
bundle-dev-tools-1.0.0/apt/
├── debs/
│   ├── git_2.43.0_amd64.deb
│   ├── curl_8.5.0_amd64.deb
│   ├── vim_2.4.0_amd64.deb
│   ├── ... (many transitive dependencies too!)
│   ├── Packages           # dpkg-scanpackages index
│   └── Packages.gz        # Gzip index (for apt update)
├── sources.list           # Local repo apt source fragment
└── pack_codename          # Pack system's codename (for mismatch warning)
	├── keys/                  # (optional) Downloaded GPG keys for audit
	│   └── kubernetes.gpg
	└── repos.json             # (optional) Repo metadata for diff/audit
```

#### Key details

- All transitive dependencies are automatically resolved and bundled
- Already-installed packages are skipped during apply (idempotent)
- Codename mismatch between pack and apply emits a warning but doesn't block
- Uses `--no-install-recommends` style via the remote apply script

---

### 5.2 dnf — RPM packages (RHEL/Rocky/Alma/CentOS)

Downloads RPM packages with dependency resolution and creates a local yum/dnf repository.

**Pack phase:** Downloads all requested RPMs with `dnf download --resolve` to a cache at
`~/.cache/syncit/dnf/`, then runs `createrepo_c` to generate repository metadata. Each task
gets its own isolated subfolder under `dnf/<task-slug>/` so multiple dnf tasks never mix RPMs.

> **Note:** syncit supports adding upstream repos automatically during `pack`
> via the `repos` field (see below). No manual repo setup needed on the online machine.

**Apply phase:** Copies RPMs to `/srv/offline/dnf/<task-slug>/rpms/`, writes a per-task
`.repo` file at `/etc/yum.repos.d/syncit-<task-slug>.repo`, and installs packages from
the local repo.

#### Manifest Task Spec

| Field                  | Required | Description                                                        |
|------------------------|----------|--------------------------------------------------------------------|
| `packages`             | Yes      | List of RPM package names to install                               |
| `repos`                | No       | List of upstream repository definitions (see below)                |
| `repos[].name`         | Yes      | Unique short name for the repo                                     |
| `repos[].baseurl`      | Yes      | Base URL of the repository                                         |
| `repos[].gpgcheck`     | No       | GPG check setting (`"1"` or `"0"`, default off)                    |
| `repos[].gpgkey`       | No       | URL to the GPG key file                                            |
| `base_installroot`     | No       | Path to a minimal OS root for accurate dep resolution (see below)  |
| `releasever`           | No       | Override the OS release version (e.g. `"9"` for Rocky 9)          |

```yaml
- name: Install system RPMs
  plugin: dnf
  packages:
    - git
    - nginx
    - python3
    - python3-pip
```

Optionally, declare upstream repositories for third-party packages:

```yaml
- name: Kubernetes components
  plugin: dnf
  repos:
    - name: kubernetes
      baseurl: https://pkgs.k8s.io/core:/stable:/v1.35/rpm/
      gpgcheck: "1"
      gpgkey: https://pkgs.k8s.io/core:/stable:/v1.35/rpm/repodata/repomd.xml.key
  packages:
    - kubeadm
    - kubelet
    - kubectl
```

During `pack`, repos are injected via `--repofrompath=<name>,<baseurl>` — no system files
are modified. GPG keys are stored in the bundle (`dnf/<slug>/keys/`) for audit and diff.
The offline `apply` phase uses the local `file://` repo only.

---

#### `base_installroot` — Accurate Dependency Resolution

By default, `dnf download --resolve` resolves dependencies against the **build host's** installed
package set. This means:

- If the build host has `systemd`, `dbus`, `glibc`, etc. already installed, DNF skips them
  — even though the fresh target VM might need a slightly different version.
- If the build host has *extra* packages not on a minimal target, those are silently omitted.

The result can be an **over- or under-bundled** package set depending on how closely the build
host matches the target.

**The fix:** point `base_installroot` to a minimal base OS root that mirrors a freshly-installed
target node. DNF then resolves as if installing into that clean environment, producing a bundle
that contains exactly the right deps for the target.

##### How to create a minimal installroot

```bash
# On your online RHEL/Rocky/Alma build host:
# 1. Create a minimal Rocky 9 installroot (only takes a few seconds)
mkdir -p /opt/syncit-roots/rocky9-minimal
sudo dnf install -y \
  --installroot /opt/syncit-roots/rocky9-minimal \
  --releasever 9 \
  @core --setopt=install_weak_deps=False

# 2. Reference it in your manifest
```

```yaml
- name: Install Kubernetes
  plugin: dnf
  base_installroot: /opt/syncit-roots/rocky9-minimal
  releasever: "9"
  repos:
    - name: kubernetes
      baseurl: https://pkgs.k8s.io/core:/stable:/v1.35/rpm/
      gpgkey: https://pkgs.k8s.io/core:/stable:/v1.35/rpm/repodata/repomd.xml.key
  packages:
    - kubeadm
    - kubelet
    - kubectl
```

With `base_installroot` set, `dnf download --resolve --installroot <path>` will:
- Treat the minimal root as the reference state
- Skip packages already in that root (`glibc`, `systemd`, `bash`, etc.)
- Only download your app's *actual* extra dependencies

> [!IMPORTANT]
> The `base_installroot` must be an **absolute path** to an existing directory on the
> packing machine. It is never transferred to the bundle — it is only used during `pack`
> for dep resolution. Create and maintain it separately on your build host.

> [!TIP]
> Reuse the same installroot across multiple pack runs for the same distro version.
> It only needs to be re-created when the target OS version changes.
> You can add it to a `Makefile` or CI script to keep it fresh:
> ```bash
> make installroot  # recreates /opt/syncit-roots/rocky9-minimal
> syncit pack bundle.yaml --output ./bundles/
> ```

##### `releasever` field

The `releasever` field overrides the OS release version used by DNF when resolving packages.
Useful when the build host runs a different major version than the target:

```yaml
  releasever: "9"    # Force Rocky/RHEL 9 resolution even on an EL8 build host
```

---

#### Full Example (Kubernetes on Rocky 9)

```yaml
apiVersion: syncit/v1
kind: Bundle
metadata:
  name: k8s-rocky9
  version: "1.35.5"
  description: "Kubernetes v1.35.5 + CRI-O for Rocky Linux 9 (offline)"
spec:
  targets:
    distro: rocky
    codename: "9"
    arch: amd64
  tasks:
    - name: Install Kubernetes packages
      plugin: dnf
      base_installroot: /opt/syncit-roots/rocky9-minimal
      releasever: "9"
      repos:
        - name: kubernetes
          baseurl: https://pkgs.k8s.io/core:/stable:/v1.35/rpm/
          gpgcheck: "1"
          gpgkey: https://pkgs.k8s.io/core:/stable:/v1.35/rpm/repodata/repomd.xml.key
      packages:
        - kubeadm
        - kubelet
        - kubectl

    - name: Install CRI-O
      plugin: dnf
      base_installroot: /opt/syncit-roots/rocky9-minimal
      releasever: "9"
      repos:
        - name: crio
          baseurl: https://download.opensuse.org/repositories/isv:/cri-o:/stable:/v1.35/rpm/
          gpgcheck: "0"
      packages:
        - cri-o
```

```bash
# One-time: create minimal installroot on build host
sudo dnf install -y --installroot /opt/syncit-roots/rocky9-minimal \
  --releasever 9 @core --setopt=install_weak_deps=False

# Pack
syncit pack bundle.yaml --output ./bundles/ --verbose

# Transfer bundle to offline Rocky 9 node and apply
syncit apply ./bundles/bundle-k8s-rocky9-1.35.5/
```

#### Bundle layout (per-task isolation)

```
bundle-k8s-rocky9-1.35.5/dnf/
├── install-kubernetes-packages/
│   ├── rpms/
│   │   ├── kubeadm-1.35.5-1.x86_64.rpm
│   │   ├── kubelet-1.35.5-1.x86_64.rpm
│   │   ├── kubectl-1.35.5-1.x86_64.rpm
│   │   ├── kubernetes-cni-1.x86_64.rpm
│   │   └── repodata/          # createrepo_c metadata
│   ├── keys/                  # (optional) Downloaded GPG keys
│   └── repos.json             # Repo metadata for diff/audit
└── install-cri-o/
    └── rpms/
        ├── cri-o-1.35.4-1.x86_64.rpm
        ├── conmon-2.1.x86_64.rpm
        ├── ... (cri-o specific transitive deps)
        └── repodata/
```

Each dnf task gets its own:
- Isolated RPM directory (`dnf/<slug>/rpms/`)
- Independent `.repo` file on the target (`/etc/yum.repos.d/syncit-<slug>.repo`)
- Separate install destination (`/srv/offline/dnf/<slug>/rpms/`)

---

### 5.3 pip — Python wheels

Downloads Python package wheels from PyPI for offline installation.

**Pack phase:** Uses `pip download -r requirements.txt` to download all wheels to
`pip/wheels/`. First tries `--only-binary=:all:` (no source compilation); if that fails,
retries without it (and warns that source distributions will need compilation on the
offline VM). Caches at `~/.cache/syncit/pip/`.

**Apply phase:** Copies wheels to `/srv/offline/pip/wheels/`, writes `/etc/pip.conf` to
disable remote indexes (`no-index = true`, `find-links` pointing to local wheelhouse),
then installs via `pip install --no-index --find-links ... -r requirements.txt`.

#### Manifest Task Spec

```yaml
- name: Python data deps
  plugin: pip
  python_version: "3.12"
  requirements: ./requirements.txt
```

| Field            | Required | Description                                    |
|------------------|----------|------------------------------------------------|
| `requirements`   | Yes*     | Path to requirements.txt (relative to manifest)|
| `pyproject`      | Yes*     | Path to pyproject.toml (Phase 2 — not yet)     |
| `python_version` | No       | Target Python version (default: `3.11`)        |

*Either `requirements` or `pyproject` must be provided.

#### Example

```yaml
# bundle.yaml
apiVersion: syncit/v1
kind: Bundle
metadata:
  name: ml-env
  version: "1.0.0"
spec:
  targets:
    distro: ubuntu
    codename: noble
    arch: amd64
  tasks:
    - name: Python deps
      plugin: pip
      python_version: "3.12"
      requirements: ./requirements.txt
```

```txt
# requirements.txt
numpy<2.0
pandas>=2.0
scikit-learn>=1.3
flask==3.0.0
requests==2.31.0
```

```bash
syncit pack bundle.yaml --output ./bundles/
# On the offline VM:
syncit apply ./bundles/bundle-ml-env-1.0.0/
```

#### Bundle layout

```
bundle-ml-env-1.0.0/pip/
├── wheels/
│   ├── numpy-1.26.3-cp312-cp312-manylinux_2_17_x86_64.whl
│   ├── pandas-2.1.4-cp312-cp312-manylinux_2_17_x86_64.whl
│   ├── scikit_learn-1.3.2-cp312-cp312-manylinux_2_17_x86_64.whl
│   ├── flask-3.0.0-py3-none-any.whl
│   ├── requests-2.31.0-py3-none-any.whl
│   └── ... (more transitive deps)
└── requirements.txt       # Copy of the original requirements file
```

#### Key details

- Prefers binary-only wheels (avoids compilation on offline VM)
- Falls back to source distributions if binary-only fails
- Python version compatibility is checked during remote apply

---

### 5.4 oci_image — Docker/OCI container images

Pulls container images using `docker`, `podman`, or `skopeo` and saves them as OCI or Docker archives for offline loading.

**Pack phase:** Preferentially uses `skopeo`, then `podman`, then `docker`. Images are pulled to the local runtime cache or `~/.cache/syncit/oci_image/` (for skopeo), then bundled into individual `.tar` archives in the bundle. Records image digests and metadata in `manifest.json`.

**Apply phase:** Detects the container runtime (`docker`, `podman`, or `ctr`) and loads
images using the appropriate command. Skips images already present (checks by source tag).

#### Manifest Task Spec

```yaml
- name: Container images
  plugin: oci_image
  images:
    - source: docker.io/library/redis:7-alpine
    - source: docker.io/library/postgres:16-alpine
    - source: docker.io/library/nginx:alpine
```

#### Example: Pack & Apply

```yaml
apiVersion: syncit/v1
kind: Bundle
metadata:
  name: container-env
  version: "1.0.0"
spec:
  targets:
    distro: ubuntu
    codename: noble
    arch: amd64
  tasks:
    - name: App images
      plugin: oci_image
      images:
        - source: docker.io/library/redis:7-alpine
        - source: docker.io/library/postgres:16-alpine
        - source: docker.io/library/nginx:alpine
        - source: docker.io/library/python:3.12-slim
```

```bash
# Online machine needs skopeo installed
sudo apt install skopeo

syncit pack bundle.yaml --output ./bundles/ -v

# On the offline VM (needs docker, podman, or ctr):
syncit apply ./bundles/bundle-container-env-1.0.0/
```

#### Bundle layout

```
bundle-container-env-1.0.0/images/
├── manifest.json                  # Image metadata + digests
├── docker.io_library_redis_7-alpine.tar
├── docker.io_library_postgres_16-alpine.tar
├── docker.io_library_nginx_alpine.tar
└── docker.io_library_python_3.12-slim.tar
```

#### manifest.json content

```json
[
  {
    "source": "docker.io/library/redis:7-alpine",
    "archive": "docker.io_library_redis_7-alpine.tar",
    "digest": "sha256:1234abcd..."
  },
  {
    "source": "docker.io/library/postgres:16-alpine",
    "archive": "docker.io_library_postgres_16-alpine.tar",
    "digest": "sha256:5678efgh..."
  }
]
```

#### Key details

- Requires `docker`, `podman`, or `skopeo` on the online machine (pack phase)
- Requires `docker`, `podman`, or `ctr` on the offline VM (apply phase)
- Supports private registries via skopeo's auth system
- Only loads images not already present in the runtime (idempotent)

---

### 5.5 npm — Node.js node_modules

Caches the full `node_modules` directory and `package-lock.json` for one or more npm projects.

**Pack phase:** Runs `npm ci` in each project directory, then copies the resulting
`node_modules/` and `package-lock.json` into the bundle under `npm/<project_name>/`.

**Apply phase:** Copies `node_modules` back to the project directory on the offline VM and
writes `.npmrc` with `offline=true` and `prefer-offline=true` settings.

#### Manifest Task Spec

```yaml
- name: Node frontend deps
  plugin: npm
  projects:
    - project_name: frontend-app
      project_dir: /home/user/my-app
    - project_name: admin-panel
      project_dir: /home/user/admin
```

| Field          | Required | Description                                    |
|----------------|----------|------------------------------------------------|
| `projects`     | Yes      | List of project definitions                    |
| `project_name` | Yes      | Unique identifier for the project              |
| `project_dir`  | Yes      | Path to project directory (with package.json)  |

#### Example

```yaml
tasks:
  - name: Frontend deps
    plugin: npm
    projects:
      - project_name: web-app
        project_dir: ./frontend
      - project_name: api-server
        project_dir: ./backend
```

```bash
# Pack (online)
syncit pack bundle.yaml --output ./bundles/

# Transfer to offline machine, then apply
syncit apply ./bundles/bundle-my-env-1.0.0/
```

#### Bundle layout

```
bundle-my-env-1.0.0/npm/
├── web-app/
│   ├── node_modules/
│   │   ├── react/
│   │   ├── express/
│   │   └── ...
│   └── package-lock.json
└── api-server/
    ├── node_modules/
    └── package-lock.json
```

#### Key details

- Requires `node` and `npm` on the online machine (pack runs `npm ci`)
- No Node.js needed on the offline VM — just copies files
- Runs `npm ci` (not `npm install`) — respects lockfiles strictly
- Writes `.npmrc` with offline flags on the target project directory

---

### 5.6 cargo — Rust vendored dependencies

Vendors Rust crate dependencies using `cargo vendor` for offline builds.

**Pack phase:** Runs `cargo vendor <vendor_dir>` in each project directory, which downloads
all crate sources and generates a vendored directory. Also saves the config snippet that cargo
vendor prints (needed to tell Cargo to use the vendored sources instead of crates.io).

**Apply phase:** Copies vendored `vendor/` directory to the project directory on the offline VM,
and writes/updates `.cargo/config.toml` to replace `crates-io` with the vendored sources.

#### Manifest Task Spec

```yaml
- name: Rust deps
  plugin: cargo
  projects:
    - project_name: my-rust-app
      project_dir: /home/user/rust-app
```

| Field          | Required | Description                                    |
|----------------|----------|------------------------------------------------|
| `projects`     | Yes      | List of project definitions                    |
| `project_name` | Yes      | Unique identifier for the project              |
| `project_dir`  | Yes      | Path to project directory (with Cargo.toml)    |

#### Example

```yaml
tasks:
  - name: Rust dependencies
    plugin: cargo
    projects:
      - project_name: backend
        project_dir: ./rust-backend
```

```bash
syncit pack bundle.yaml --output ./bundles/
# Then on offline VM:
syncit apply ./bundles/bundle-my-env-1.0.0/
```

#### Bundle layout

```
bundle-my-env-1.0.0/cargo/
└── my-rust-app/
    ├── vendor/
    │   ├── serde-1.0./
    │   ├── tokio-1.35./
    │   └── ... (all vendored crates with sources)
    └── config.toml.snippet   # Cargo config to use vendored sources
```

The `.cargo/config.toml` on the offline VM will be configured with:

```toml
[source.crates-io]
replace-with = "vendored-sources"

[source.vendored-sources]
directory = "vendor"
```

#### Key details

- Requires `cargo` on the online machine (pack runs `cargo vendor`)
- No Rust toolchain needed on the offline VM for copy operations
- The `.cargo/config.toml` is only written if `replace-with` isn't already configured
- Source distribution vendoring means the offline VM still needs Rust to build

---

### 5.7 go — Go module cache

Downloads all Go module dependencies and bundles the `GOMODCACHE` for offline builds.

**Pack phase:** Runs `go mod download ./...` to seed the global cache, then re-runs with
`GOMODCACHE` pointing to the bundle directory to populate it with module sources.

**Apply phase:** Copies the module cache to `/opt/offline/go/modcache/` and writes
`/etc/profile.d/offline-go.sh` which exports `GOMODCACHE`, `GOPROXY=off`, and
`GONOSUMCHECK=*` so all Go builds use the local cache without network access.

#### Manifest Task Spec

```yaml
- name: Go deps
  plugin: go
  projects:
    - project_name: my-service
      project_dir: /home/user/go-service
```

| Field          | Required | Description                                    |
|----------------|----------|------------------------------------------------|
| `projects`     | Yes      | List of project definitions                    |
| `project_name` | Yes      | Unique identifier for the project              |
| `project_dir`  | Yes      | Path to project directory (with go.mod)        |

#### Example

```yaml
tasks:
  - name: Go modules
    plugin: go
    projects:
      - project_name: api-gateway
        project_dir: ./api-gateway
      - project_name: auth-service
        project_dir: ./auth-service
```

```bash
syncit pack bundle.yaml --output ./bundles/
# On offline VM:
syncit apply ./bundles/bundle-my-env-1.0.0/
```

#### Bundle layout

```
bundle-my-env-1.0.0/go/
└── modcache/
    ├── cache/
    │   └── download/...
    ├── github.com/
    │   ├── gin-gonic/
    │   ├── golang/
    │   └── ...
    ├── golang.org/
    └── ...
```

#### Key details

- Requires `go` on the online machine
- On the offline VM, a profile script sets `GOMODCACHE`, `GOPROXY=off`, `GONOSUMCHECK=*`
- All Go projects share a single module cache (not per-project like npm/cargo)
- System paths require root/sudo on the offline VM for apply

---

### 5.8 file — Arbitrary files and archives

Downloads arbitrary files from URLs (tarballs, binaries, config files) and optionally
extracts archives.

**Pack phase:** Downloads each URL to `file/<filename>` in the bundle. Caches at
`~/.cache/syncit/file/`.

**Apply phase:** Copies each file to its configured `dest` path. Supports:
- Plain file copy
- Archive extraction (`.tar.gz`, `.tgz`, `.tar`, `.zip`)
- `strip_components` to skip leading directory levels in archives
- `executable` flag to set +x permission

#### Manifest Task Spec

```yaml
tasks:
  - name: Download binaries
    plugin: file
    files:
      - url: https://github.com/prometheus/prometheus/releases/download/v2.45.0/prometheus-2.45.0.linux-amd64.tar.gz
        dest: /opt/prometheus
        extract: true
        strip_components: 1

      - url: https://github.com/mikefarah/yq/releases/download/v4.40.5/yq_linux_amd64
        dest: /usr/local/bin/yq
        executable: true
```

| Field                     | Required | Type    | Description                                    |
|---------------------------|----------|---------|------------------------------------------------|
| `files`                   | Yes      | list    | List of file download specs                    |
| `url`                     | Yes      | string  | Source URL to download from                    |
| `dest`                    | Yes      | string  | Destination path on the target system          |
| `extract`                 | No       | bool    | If true, extract archive into `dest` directory |
| `strip_components`        | No       | int     | Number of leading path components to strip during extraction |
| `executable`              | No       | bool    | If true, set chmod +x on copied file          |

#### Example

```yaml
apiVersion: syncit/v1
kind: Bundle
metadata:
  name: tools-bundle
  version: "1.0.0"
spec:
  targets:
    distro: ubuntu
    codename: noble
    arch: amd64
  tasks:
    - name: Prometheus monitoring
      plugin: file
      files:
        - url: https://github.com/prometheus/prometheus/releases/download/v2.45.0/prometheus-2.45.0.linux-amd64.tar.gz
          dest: /opt/prometheus
          extract: true
          strip_components: 1

    - name: CLI tools
      plugin: file
      files:
        - url: https://github.com/mikefarah/yq/releases/download/v4.40.5/yq_linux_amd64
          dest: /usr/local/bin/yq
          executable: true
        - url: https://github.com/stedolan/jq/releases/download/jq-1.6/jq-linux64
          dest: /usr/local/bin/jq
          executable: true
```

```bash
syncit pack bundle.yaml --output ./bundles/
# Transfer to offline VM
syncit apply ./bundles/bundle-tools-bundle-1.0.0/
```

#### Bundle layout

```
bundle-tools-bundle-1.0.0/file/
├── prometheus-2.45.0.linux-amd64.tar.gz
├── yq_linux_amd64
└── jq-linux64
```

#### Key details

- No external tools required — uses Python `urllib` for downloads
- Supports archive extraction with strip_components (unlike most other plugins)
- Can make files executable automatically
- Good for downloading precompiled binaries that don't need package managers

---

## 6. Roles System

Roles allow you to define reusable groups of tasks in separate `role.yaml` files and reference
them from multiple manifests.

### Role File Format

```yaml
# roles/dev-tools.yaml
name: dev-tools
description: Common development tools
version: "1.0.0"
tasks:
  - plugin: apt
    name: system-tools
    spec:
      packages: [git, curl, vim, build-essential]

  - plugin: pip
    name: python-tools
    spec:
      requirements: ./requirements.txt
      python_version: "3.12"
```

### Referencing Roles in a Manifest

```yaml
apiVersion: syncit/v1
kind: Bundle
metadata:
  name: app-env
  version: "2.0.0"
spec:
  targets:
    distro: ubuntu
    codename: noble
    arch: amd64
  roles:
    - path: ./roles/dev-tools.yaml    # Path relative to bundle.yaml
    - path: ./roles/database.yaml
  tasks:
    - name: App-specific config
      plugin: file
      files:
        - url: https://example.com/app-config.tar.gz
          dest: /opt/app
          extract: true
```

### How Role Expansion Works

1. Role tasks are processed first (in their file order)
2. Inline tasks (from `spec.tasks`) come after role tasks
3. Duplicate task names (same `plugin + name` combination) raise a ValueError
4. The merged task list is what commands like `pack`, `apply`, and `validate` use

---

## 7. Inventory & Remote Hosts

The inventory file defines target VMs for remote operations (`syncit exec`, `syncit apply --bundle`, `syncit up`).

### Inventory File Format

```yaml
# inv.yaml
hosts:
  web-01:
    host: 10.0.1.101
    user: ubuntu
    ssh_key: ~/.ssh/id_rsa
    bundle_dest: /opt/bundles/
    state_file: /opt/syncit/state.json

  web-02:
    host: 10.0.1.102
    user: ubuntu
    ssh_key: ~/.ssh/id_rsa

  db-01:
    host: 10.0.2.201
    user: admin
    ssh_key: ~/.ssh/db-key

groups:
  webservers:
    - web-01
    - web-02
  databases:
    - db-01
```

### Host Fields

| Field         | Required | Default                | Description                              |
|---------------|----------|------------------------|------------------------------------------|
| `host`        | Yes      | —                      | Hostname or IP address                   |
| `user`        | Yes      | —                      | SSH username                             |
| `ssh_key`     | No       | Default SSH key        | Path to SSH private key                  |
| `bundle_dest` | No       | `/opt/bundles/`        | Remote directory for bundle transfer     |
| `state_file`  | No       | `/opt/syncit/state.json` | Remote state file path                 |

### Group Fields

| Field     | Required | Description                                      |
|-----------|----------|--------------------------------------------------|
| (name)    | Yes      | Group name references a list of host IDs         |

### Example Usage

```bash
# Execute on a single host
syncit exec -i inv.yaml -t web-01 -- df -h

# Execute on a group
syncit exec -i inv.yaml -g webservers -- sudo systemctl status nginx

# Execute on all hosts
syncit exec -i inv.yaml --all -- uptime

# Remote apply on a single host
syncit apply --bundle bundle.tar.gz -i inv.yaml -t web-01

# Remote apply on a whole group
syncit up bundle.yaml -i inv.yaml -g webservers
```

---

## 8. Bundle Structure

A bundle is a self-contained directory (or archive) produced by `syncit pack`.

```
bundle-<name>-<version>/
├── bundle.yaml             # Copy of the original manifest
├── bundle.meta.json        # Metadata (version, created_at, targets, tasks)
├── apt/                    # apt plugin (if used)
│   ├── debs/
│   │   ├── Packages
│   │   ├── Packages.gz
│   │   └── *.deb
│   ├── sources.list
│   ├── pack_codename
│   ├── keys/              # (optional) Downloaded GPG keys
│   └── repos.json         # (optional) Repo metadata for diff/audit
├── dnf/                    # dnf plugin (if used)
│   ├── rpms/
│   │   ├── *.rpm
│   │   └── repodata/
│   ├── keys/              # (optional) Downloaded GPG keys
│   └── repos.json         # (optional) Repo metadata for diff/audit
├── pip/                    # pip plugin (if used)
│   ├── wheels/
│   │   └── *.whl
│   └── requirements.txt
├── images/                 # oci_image plugin (if used)
│   ├── manifest.json
│   └── *.tar
├── npm/                    # npm plugin (if used)
│   └── <project_name>/
│       ├── node_modules/
│       └── package-lock.json
├── cargo/                  # cargo plugin (if used)
│   └── <project_name>/
│       ├── vendor/
│       └── config.toml.snippet
├── go/                     # go plugin (if used)
│   └── modcache/
│       └── ...
└── file/                   # file plugin (if used)
    └── <downloaded files>
```

### bundle.meta.json Format

```json
{
  "name": "my-env",
  "version": "1.0.0",
  "created_at": "2026-06-03T12:00:00Z",
  "syncit_version": "0.1.0",
  "targets": {
    "distro": "ubuntu",
    "codename": "noble",
    "arch": "amd64"
  },
  "tasks": [
    {
      "name": "Install system packages",
      "plugin": "apt",
      "status": "packed",
      "artifact_count": 87
    },
    {
      "name": "Install Python deps",
      "plugin": "pip",
      "status": "packed",
      "artifact_count": 12
    }
  ]
}
```

---

## 9. State & Idempotency

syncit tracks what was applied so repeated `syncit apply` calls are safe and efficient.

### Local Apply State

When applying locally, state is stored at `/opt/syncit/state.json` (configurable via
`--state-file`). This JSON file tracks each task's checksum and status:

```json
{
  "applied_tasks": {
    "Install system packages": {
      "checksum": "sha256:abc123...",
      "status": "success"
    }
  }
}
```

- A task is **skipped** if its checksum matches a previous successful run
- Use `--force` to re-apply regardless of state
- Use `--dry-run` to see what would change without executing
- Use `--continue-on-error` to keep going if a plugin fails

### Remote Apply (Smart Apply) State

When applying remotely via SSH (`syncit apply --bundle` or `syncit up`), state is stored
on the remote VM at the path specified in the inventory's `state_file` field
(default: `/opt/syncit/state.json`). The checksum is computed from all files in the
bundle's plugin artifact directory. Tasks with matching checksums and success status are
skipped.

---

## 10. Zero-Dependency Remote Apply

syncit can apply bundles to remote VMs **without having syncit installed on the target**.
It works by generating a self-contained bash script per task and piping it over SSH.

### How It Works

1. **Generate script**: Each plugin's `render_apply_sh()` returns a bash snippet that
   performs the apply using only native OS tools
2. **Transfer**: The bundle archive is SCP'd to the remote VM
3. **Extract**: SSH commands extract the archive on the remote VM
4. **Execute**: Each task's bash script is piped via `sudo bash -s` over SSH
5. **State**: After each task succeeds, the checksum is written to the remote state file
6. **Diff check**: Before running each task, the system checks if the task's artifacts
   and checksum match a previously successful state — if so, it's skipped

### Using Remote Apply

```bash
# Direct apply from archive
syncit apply --bundle ./bundle-my-env-1.0.0.tar.gz -i inv.yaml -t web-01

# Pack and apply in one command
syncit up bundle.yaml -i inv.yaml -t web-01

# Apply to a group of hosts
syncit up bundle.yaml -i inv.yaml -g webservers

# Apply to all hosts
syncit up bundle.yaml -i inv.yaml --all

# Preview the generated script without running anything
syncit apply --bundle ./bundle.tar.gz --print-script
```

### What plugins support remote apply?

All plugins implement `render_apply_sh()` for zero-dependency remote apply:

| Plugin     | Remote apply dependencies on target     |
|------------|-----------------------------------------|
| **apt**    | `apt-get`, `sudo`                       |
| **dnf**    | `dnf`, `createrepo_c`, `sudo`           |
| **pip**    | `pip3`, `python3`                       |
| **oci_image** | `docker`/`podman`/`ctr`, `sudo`     |
| **npm**    | None (just `cp` and `echo`)             |
| **cargo**  | None (just `cp`, `mkdir`, `cat`)        |
| **go**     | None (just `cp`, `mkdir`, `echo`)       |
| **file**   | None (just `cp`, `mkdir`, `chmod`, `tar`, `unzip`) |

---

## 11. End-to-End Workflow Examples

### Example 1: Full Stack Web Server

This example packs everything needed for a Python web app with Redis and Nginx.

**bundle.yaml:**
```yaml
apiVersion: syncit/v1
kind: Bundle
metadata:
  name: web-stack
  version: "1.0.0"
  description: Full stack web deployment
spec:
  targets:
    distro: ubuntu
    codename: noble
    arch: amd64
  tasks:
    - name: System packages
      plugin: apt
      packages:
        - git
        - curl
        - nginx
        - python3.12
        - python3.12-venv
        - python3-pip
        - redis-server

    - name: Python dependencies
      plugin: pip
      python_version: "3.12"
      requirements: ./requirements.txt

    - name: Container images
      plugin: oci_image
      images:
        - source: docker.io/library/redis:7-alpine
        - source: docker.io/library/python:3.12-slim

    - name: CLI tools
      plugin: file
      files:
        - url: https://github.com/mikefarah/yq/releases/download/v4.40.5/yq_linux_amd64
          dest: /usr/local/bin/yq
          executable: true
```

**requirements.txt:**
```
fastapi==0.109.0
uvicorn==0.27.0
redis==5.0.1
sqlalchemy==2.0.25
psycopg2-binary==2.9.9
```

**Workflow:**
```bash
# Step 1: On the online machine
sudo apt install dpkg-dev skopeo                    # Install required tools
pip install .                                       # Install syncit

# Step 2: Validate and pack
syncit validate bundle.yaml
syncit pack bundle.yaml --output ./bundles/ -v

# Step 3: Transfer to offline VM
scp -r ./bundles/bundle-web-stack-1.0.0/ ubuntu@offline-vm:/tmp/

# Step 4: On the offline VM
syncit apply /tmp/bundle-web-stack-1.0.0/

# Or, do it remotely in one step:
syncit up bundle.yaml -i inv.yaml -t web-server
```

### Example 2: RPM-Based Application Server

For RHEL/Rocky/Alma Linux:

**bundle.yaml:**
```yaml
apiVersion: syncit/v1
kind: Bundle
metadata:
  name: app-server
  version: "1.0.0"
spec:
  targets:
    distro: rocky
    codename: "9"
    arch: amd64
  tasks:
    - name: System RPMs
      plugin: dnf
      packages:
        - git
        - nginx
        - python3
        - python3-pip
        - podman

    - name: Python wheels
      plugin: pip
      python_version: "3.9"
      requirements: ./requirements.txt
```

```bash
sudo dnf install createrepo_c skopeo
syncit pack bundle.yaml --output ./bundles/
# Transfer to offline RHEL/Rocky system...
syncit apply ./bundles/bundle-app-server-1.0.0/
```

### Example 3: Rust + Go + Node Full Stack

When you need multiple language ecosystems:

**bundle.yaml:**
```yaml
apiVersion: syncit/v1
kind: Bundle
metadata:
  name: full-stack-dev
  version: "1.0.0"
spec:
  targets:
    distro: ubuntu
    codename: noble
    arch: amd64
  tasks:
    - name: System deps
      plugin: apt
      packages:
        - git
        - build-essential
        - pkg-config
        - libssl-dev

    - name: Rust crates
      plugin: cargo
      projects:
        - project_name: backend
          project_dir: ./rust-backend

    - name: Go modules
      plugin: go
      projects:
        - project_name: api-gateway
          project_dir: ./go-api

    - name: npm packages
      plugin: npm
      projects:
        - project_name: frontend
          project_dir: ./node-frontend
```

### Example 4: Using Roles for Reusability

**roles/base-server.yaml:**
```yaml
name: base-server
description: Base packages for any server
version: "1.0.0"
tasks:
  - plugin: apt
    name: base-packages
    spec:
      packages: [curl, git, vim, htop, tmux, ufw]
```

**roles/python-app.yaml:**
```yaml
name: python-app
description: Python runtime for web apps
version: "1.0.0"
tasks:
  - plugin: apt
    name: python-runtime
    spec:
      packages: [python3.12, python3.12-venv, python3-pip]
  - plugin: pip
    name: app-deps
    spec:
      python_version: "3.12"
      requirements: ./app-requirements.txt
```

**bundle.yaml:**
```yaml
apiVersion: syncit/v1
kind: Bundle
metadata:
  name: production-app
  version: "2.1.0"
spec:
  targets:
    distro: ubuntu
    codename: noble
    arch: amd64
  roles:
    - path: ./roles/base-server.yaml
    - path: ./roles/python-app.yaml
  tasks:
    - name: App containers
      plugin: oci_image
      images:
        - source: docker.io/library/nginx:alpine
```

### Example 5: Remote Apply in Action

```yaml
# inv.yaml
hosts:
  prod-01:
    host: 10.0.1.10
    user: admin
    ssh_key: ~/.ssh/prod-key
  prod-02:
    host: 10.0.1.11
    user: admin
    ssh_key: ~/.ssh/prod-key
  staging:
    host: 10.0.2.20
    user: deploy
    ssh_key: ~/.ssh/staging-key

groups:
  production:
    - prod-01
    - prod-02
```

```bash
# Pack and deploy to staging in one command
syncit up bundle.yaml -i inv.yaml -t staging

# Or step by step for production rollout:
syncit pack bundle.yaml --format tar.gz -o ./bundles/
syncit apply --bundle ./bundles/bundle-my-env-1.0.0.tar.gz -i inv.yaml -t prod-01
syncit apply --bundle ./bundles/bundle-my-env-1.0.0.tar.gz -i inv.yaml -t prod-02

# Check the state on a remote host
syncit exec -i inv.yaml -t prod-01 -- sudo cat /opt/syncit/state.json | python3 -m json.tool

# Run arbitrary commands across all production hosts
syncit exec -i inv.yaml -g production -- sudo systemctl status nginx --no-pager

# Compare two bundle versions
syncit diff ./bundles/bundle-my-env-1.0.0/ ./bundles/bundle-my-env-2.0.0/
```

### Example 6: Binary-Only Pipeline

Download pre-compiled binaries without any system packages:

```yaml
apiVersion: syncit/v1
kind: Bundle
metadata:
  name: binary-tools
  version: "1.0.0"
spec:
  targets:
    distro: ubuntu
    codename: noble
    arch: amd64
  tasks:
    - name: Monitoring stack
      plugin: file
      files:
        - url: https://github.com/prometheus/prometheus/releases/download/v2.45.0/prometheus-2.45.0.linux-amd64.tar.gz
          dest: /opt/prometheus
          extract: true
          strip_components: 1
        - url: https://github.com/prometheus/node_exporter/releases/download/v1.6.1/node_exporter-1.6.1.linux-amd64.tar.gz
          dest: /opt/node_exporter
          extract: true
          strip_components: 1
        - url: https://github.com/grafana/loki/releases/download/v2.9.0/loki-linux-amd64.zip
          dest: /opt/loki
          extract: true
          strip_components: 0

    - name: CLI utilities
      plugin: file
      files:
        - url: https://github.com/mikefarah/yq/releases/download/v4.40.5/yq_linux_amd64
          dest: /usr/local/bin/yq
          executable: true
        - url: https://github.com/stedolan/jq/releases/download/jq-1.6/jq-linux64
          dest: /usr/local/bin/jq
          executable: true
        - url: https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64
          dest: /usr/local/bin/cloudflared
          executable: true
```

---

## Tips & Best Practices

1. **Always validate first** — `syncit validate bundle.yaml` catches typos and missing
   fields before you spend time packing

2. **Use dry-run** — `syncit pack --dry-run` and `syncit apply --dry-run` show what would
   happen without executing anything

3. **Use versioning** — Bump the `version` field in metadata when your requirements change;
   `syncit diff` makes it easy to see exactly what changed between versions

4. **Use roles for shared config** — Define common tasks once in role files and reuse
   across multiple manifests

5. **Use `syncit up` for fast iteration** — Pack and apply in one command during development

6. **Use `--verbose` for debugging** — See exactly what commands are being run and what
   artifacts are produced

7. **Cache directory** — All plugins cache downloads at `~/.cache/syncit/<plugin>/` to
   speed up repeated pack operations. Use `--no-cache` to force fresh downloads

8. **Bundle archives** — Use `--format tar.gz` or `--format zip` for easier transfer;
   the archive contains the same structure as the directory

9. **Permissions** — The local apply and remote apply may need `sudo` access for system
   operations (installing packages, writing to `/etc/`, etc.)

10. **Codename awareness** — The apt plugin records the pack machine's codename and warns
    on mismatch during apply. Pack on the same OS version as your target for best results

11. **Third-party repos** — For packages outside default OS repos (Kubernetes, Docker, Grafana,
    etc.), add the upstream repo on the online machine **before** running `syncit pack`. The
    offline VM gets the downloaded packages from the bundle — no remote repos needed there.
    See [Repository Requirements](#repository-requirements-for-third-party-packages) for examples.