#!/usr/bin/env python3
"""
Test oci_image plugin pack + apply with 3 small images.
Uses podman (skopeo not available locally) to validate the fallback path:
  pack  → skopeo copy docker://<src> oci-archive:<tar>   (via skopeo)
  apply → skopeo copy (if available) OR podman load + explicit tag (fallback)

This validates that images never end up as <none>:<none>.
"""

import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

# ── Test images (small, fast to pull) ───────────────────────────────────────
TEST_IMAGES = [
    "quay.io/libpod/busybox:latest",
    "quay.io/libpod/alpine:latest",
]

# ── Helpers ──────────────────────────────────────────────────────────────────
def run(cmd, **kw):
    print(f"  $ {' '.join(cmd)}")
    r = subprocess.run(cmd, capture_output=True, text=True, **kw)
    if r.stdout.strip():
        print(f"    stdout: {r.stdout.strip()[:200]}")
    if r.stderr.strip():
        print(f"    stderr: {r.stderr.strip()[:200]}")
    return r

def section(title):
    print(f"\n{'='*60}")
    print(f"  {title}")
    print(f"{'='*60}")

def ok(msg):   print(f"  ✅  {msg}")
def fail(msg): print(f"  ❌  {msg}"); sys.exit(1)
def warn(msg): print(f"  ⚠️   {msg}")

# ── 1. PACK ──────────────────────────────────────────────────────────────────
section("PACK: skopeo copy each image to oci-archive")

tmpdir = Path(tempfile.mkdtemp(prefix="oci_image_test_"))
image_dir = tmpdir / "images"
image_dir.mkdir()
print(f"  Working dir: {tmpdir}")

manifest = []

def safe_name(src):
    return src.replace("/", "_").replace(":", "_")

has_skopeo = shutil.which("skopeo") is not None
has_podman = shutil.which("podman") is not None

if not has_podman and not has_skopeo:
    fail("Neither podman nor skopeo found — cannot run test")

for src in TEST_IMAGES:
    safe = safe_name(src)
    archive = image_dir / f"{safe}.tar"
    print(f"\n  Pulling: {src}")

    if has_skopeo:
        r = run(["skopeo", "copy", f"docker://{src}", f"oci-archive:{archive}"])
    else:
        # Use podman save (docker-archive format) since skopeo isn't available
        # Pull first then save to oci-archive format emulation via podman
        run(["podman", "pull", src])
        r = run(["podman", "save", "--format=oci-archive", "-o", str(archive), src])

    if r.returncode != 0:
        fail(f"Pack failed for {src}")

    size_mb = archive.stat().st_size / 1024 / 1024
    ok(f"Saved {src} → {archive.name} ({size_mb:.1f} MB)")
    manifest.append({"source": src, "archive": f"{safe}.tar", "digest": "sha256:test"})

# Write manifest.json
manifest_file = image_dir / "manifest.json"
manifest_file.write_text(json.dumps(manifest, indent=2))
ok(f"Wrote manifest.json with {len(manifest)} entries")

# ── 2. VERIFY: each tar is a separate, unique archive ────────────────────────
section("VERIFY: each .tar is unique (no collapsed duplicates)")

sizes = {}
for entry in manifest:
    p = image_dir / entry["archive"]
    sizes[entry["source"]] = p.stat().st_size

unique_sizes = len(set(sizes.values()))
print(f"  Archive sizes:")
for src, sz in sizes.items():
    print(f"    {src}: {sz/1024/1024:.1f} MB")

if unique_sizes == 1 and len(TEST_IMAGES) > 1:
    fail("ALL tars are the same size — likely collapsed duplicate (the 87MB bug!)")
else:
    ok(f"All {len(TEST_IMAGES)} archives are distinct sizes — no collapse bug")

# ── 3. APPLY: load via podman, verify tags ────────────────────────────────────
section("APPLY: load images via podman, verify tags are correct")

# Remove images first (clean state)
for src in TEST_IMAGES:
    run(["podman", "rmi", "-f", src])

errors = []
for entry in manifest:
    src = entry["source"]
    archive = image_dir / entry["archive"]
    print(f"\n  Loading: {src}")

    if has_skopeo:
        r = run(["skopeo", "copy", f"oci-archive:{archive}", f"containers-storage:{src}"])
        if r.returncode != 0:
            errors.append(f"skopeo copy failed for {src}: {r.stderr.strip()}")
            continue
    else:
        # Fallback: podman load + explicit tag
        r = run(["podman", "load", "-i", str(archive)])
        if r.returncode != 0:
            errors.append(f"podman load failed for {src}: {r.stderr.strip()}")
            continue

        # Parse loaded ref from output
        loaded_ref = None
        for line in r.stdout.splitlines():
            line = line.strip()
            if line.startswith("Loaded image:"):
                loaded_ref = line.split(":", 1)[1].strip()
                break
            if line.startswith("Loaded image ID:"):
                loaded_ref = line.split(":", 2)[-1].strip()
                break

        print(f"    podman load output ref: {loaded_ref!r}")
        if loaded_ref and loaded_ref != src:
            print(f"    → Tagging '{loaded_ref}' as '{src}'")
            r2 = run(["podman", "tag", loaded_ref, src])
            if r2.returncode != 0:
                errors.append(f"tag failed for {src}: {r2.stderr.strip()}")
                continue

    # Verify the image is now accessible under the expected name
    r3 = run(["podman", "images", "--format", "{{.Repository}}:{{.Tag}}"])
    if src in r3.stdout:
        ok(f"'{src}' is correctly tagged in podman images")
    else:
        errors.append(f"'{src}' NOT found in podman images after load!")

# ── 4. CHECK: no <none>:<none> images ────────────────────────────────────────
section("CHECK: no <none>:<none> images in store")

r = run(["podman", "images"])
if "<none>" in r.stdout:
    lines = [l for l in r.stdout.splitlines() if "<none>" in l]
    warn(f"Found <none>:<none> images:\n    " + "\n    ".join(lines))
    errors.append("<none>:<none> images present after load")
else:
    ok("No <none>:<none> images — clean load!")

# ── 5. CHECK: unique Image IDs ────────────────────────────────────────────────
section("CHECK: each image has a unique Image ID (no 87MB collapse bug)")

r = run(["podman", "images", "--format", "{{.Repository}}:{{.Tag}} {{.ID}}"])
id_map = {}
for line in r.stdout.strip().splitlines():
    parts = line.rsplit(" ", 1)
    if len(parts) == 2:
        name, img_id = parts
        if any(name == src for src in TEST_IMAGES):
            id_map[name] = img_id

print(f"  Image IDs:")
for name, img_id in id_map.items():
    print(f"    {name}: {img_id}")

unique_ids = len(set(id_map.values()))
if unique_ids < len(TEST_IMAGES) and len(id_map) == len(TEST_IMAGES):
    fail(f"Only {unique_ids} unique Image IDs for {len(TEST_IMAGES)} images — DUPLICATE BUG!")
else:
    ok(f"All {unique_ids} Image IDs are unique — no duplicate/collapse bug")

# ── RESULT ────────────────────────────────────────────────────────────────────
section("RESULT")

if errors:
    for e in errors:
        print(f"  ❌  {e}")
    fail(f"{len(errors)} error(s) — test FAILED")
else:
    ok(f"All {len(TEST_IMAGES)} images loaded correctly with proper tags and unique IDs")

# Cleanup
shutil.rmtree(tmpdir)
for src in TEST_IMAGES:
    run(["podman", "rmi", "-f", src])

print("\n  🎉 Test PASSED\n")
