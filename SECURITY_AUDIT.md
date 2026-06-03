# Security Audit Report — syncit

**Date:** 2026-06-03  
**Scope:** All 37 Python files in `syncit/`  
**Audit type:** Manual code review across 10 vulnerability categories

---

## Summary

| Severity | Count | Key Areas |
|----------|-------|-----------|
| 🔴 HIGH | 4 | SSRF, Zip Slip, path traversal, arbitrary file write |
| 🟡 MEDIUM | 4 | Command injection in remote scripts, SSH key leakage, repo name traversal, temp leak |
| 🟢 LOW | 5 | Input constraints, archive protection, registry validation, checksums, download limits |

---

## 🔴 HIGH

### H1. SSRF / Local File Read via `urllib.request.urlretrieve`

**Files:** `syncit/plugins/file.py:71`, `syncit/plugins/apt.py:134`, `syncit/plugins/dnf.py:121`

`urllib.request.urlretrieve()` supports the `file://` scheme. A manifest with URLs like `file:///etc/shadow` would copy local files into the bundle during `pack`. This affects:
- `file` plugin — file downloads from manifest URLs
- `apt` plugin — GPG key downloads via `gpg_key` field
- `dnf` plugin — GPG key downloads via `gpgkey` field

**Fix:** Validate URL scheme — reject anything that is not `http` or `https`.

### H2. Zip Slip in Archive Extraction

**File:** `syncit/bundle/archive.py:58-60`

`ZipFile.extractall()` extracts all members without path traversal checks. A malicious `.zip` archive with entries like `../../../etc/cron.d/malicious` would write outside the target directory. The tar path has a `safe_extract()` guard, but the zip path does not.

**Fix:** Validate each member name against path traversal before extracting.

### H3. Path Traversal via Manifest `name` / `version`

**Files:** `syncit/commands/pack.py:69-77`, `syncit/bundle/bundle.py:59`

The bundle directory name is constructed as `f"bundle-{name}-{version}"`. If `name` contains `../`, the resolved path escapes the intended output directory.

**Fix:** Validate that `name` and `version` contain no path separators.

### H4. Arbitrary File Write via `dest` in File Plugin

**File:** `syncit/plugins/file.py:98`

The `dest` field from the manifest is used directly as `Path(f["dest"]).expanduser()` with no restrictions. On the offline VM, this allows writing downloaded files to any path — including `/etc/sudoers`, `~/.ssh/authorized_keys`, etc.

**Fix:** Restrict `dest` to a set of allowed base directories, or at minimum add a prominent warning.

---

## 🟡 MEDIUM

### M1. Unquoted Package Names in Generated Bash Scripts

**Files:** `syncit/plugins/apt.py:492`, `syncit/plugins/dnf.py:277`

`render_apply_sh()` interpolates package names directly into bash strings. If a package name contained shell metacharacters (`` ` ``, `$(...)`, `;`), the remote apply script would interpret them. These scripts are piped to `sudo bash -s` over SSH.

**Fix:** Use `shlex.quote()` on each value before interpolating into bash scripts.

### M2. SSH Key Paths Visible in Process Listings

**Files:** `syncit/commands/apply.py:126-128`, `syncit/commands/exec_cmd.py:133-142`

SSH private key paths are passed via the `-i` flag on the command line. On shared systems, other users can see key paths in `/proc/<pid>/cmdline`.

**Fix:** Use a temporary SSH config file (`-F`) instead of command-line `-i`, or document the risk.

### M3. Repo Name Path Traversal

**Files:** `syncit/plugins/apt.py:129,160`, `syncit/plugins/dnf.py:117`

Repo `name` from the manifest is used directly in file path construction (e.g., `keys_dir / f"{name}.gpg"`). A name like `../../etc/sudoers` could write outside the intended directory.

**Fix:** Sanitize repo names to alphanumeric characters plus `._-`.

### M4. Temp Directory Leak in `run_pack`

**File:** `syncit/commands/pack.py:72-167`

When `format != "dir"`, a temp directory is created via `mkdtemp()` but cleanup only happens on two specific return paths. An unhandled exception between creation and cleanup would leak the temp directory.

**Fix:** Wrap temp directory usage in a `try/finally` block.

---

## 🟢 LOW

### L1. No Field-Level Constraints on Manifest Schema

**File:** `syncit/manifest/schema.py:8-12`

`ManifestMetadata.name`, `version`, etc. are unconstrained `str` fields with no `max_length`, `min_length`, or `pattern` validators.

### L2. Zip Extraction Lacks Safe Member Check

**File:** `syncit/bundle/archive.py:60`

Same as H2 — noted separately for tracking.

### L3. OCI Image Source Not Validated

**File:** `syncit/plugins/oci_image.py:123,133`

No validation on image `source` field — could reference internal registries.

### L4. No Checksum Verification on Downloads

**Files:** `syncit/plugins/file.py`, `syncit/plugins/apt.py`, `syncit/plugins/dnf.py`, `syncit/plugins/oci_image.py`

No plugin verifies that downloaded files match expected checksums or signatures.

### L5. No Size/Timeout Limits on URL Downloads

**File:** `syncit/plugins/file.py:71`

`urllib.request.urlretrieve()` is called with no timeout or size limits.

---

## ✅ Done Well

1. **All YAML loading uses `yaml.safe_load()`** — no insecure deserialization
2. **All subprocess calls use list form** — no `shell=True`, no trivial shell injection
3. **SSH commands in `exec_cmd.py` use `shlex.quote()`** properly
4. **Tarfile extraction in `archive.py` has `safe_extract()`** with path boundary checks
5. **No hardcoded credentials or secrets found** in any source file
6. **Temp directory cleanup in `up.py`** uses proper `try/finally` pattern
7. **Pydantic validation on manifest structure** — `apiVersion`, `kind` validated