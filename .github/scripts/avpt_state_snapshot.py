#!/usr/bin/env python3
"""AVPT Preserve-beat state snapshot for the Odisena Console static PWA.

Emits a deterministic, read-only JSON snapshot of the deploy-governing config
state (Vercel / Netlify / Cloudflare headers, the PWA data index, and the
service-worker cache key). It is the `snapshot_command` for the shared AVPT
reusable workflow's Preserve step: it captures the "highest known-good"
configuration surface so a promotion is reversible against a recorded baseline.

Safety contract (matches the other .github/scripts checks):
  - stdlib only, no network, no secrets, no environment mutation
  - read-only: never writes, deploys, or rolls back anything
  - deterministic: same tree in -> same JSON out (sorted keys, sha256 hashes)

Prints JSON to stdout and exits 0 when every tracked config file is present,
1 if any is missing (a missing deploy-config file is a real snapshot fault).
"""
import hashlib
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

# Files that define how/what the static site deploys. These are the surfaces a
# reversible promotion must be able to restore to a known-good baseline.
CONFIG_FILES = [
    "vercel.json",
    "netlify.toml",
    "_headers",
    "catalog.json",
    "manifest.webmanifest",
    "sw.js",
    "index.html",
    "404.html",
]

CACHE_RE = re.compile(r"const\s+CACHE\s*=\s*['\"]([^'\"]+)['\"]")


def sha256_file(path: Path, chunk: int = 65536) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(chunk), b""):
            h.update(block)
    return h.hexdigest()


def sw_cache_key() -> str | None:
    sw = ROOT / "sw.js"
    if not sw.exists():
        return None
    m = CACHE_RE.search(sw.read_text(encoding="utf-8"))
    return m.group(1) if m else None


def main() -> int:
    files = []
    missing = []
    for rel in CONFIG_FILES:
        p = ROOT / rel
        if p.exists():
            files.append({"path": rel, "size": p.stat().st_size, "sha256": sha256_file(p)})
        else:
            missing.append(rel)

    files.sort(key=lambda e: e["path"])

    # A stable hash over (path, content-hash) pairs: a single fingerprint of the
    # whole deploy-config surface for quick baseline-to-baseline comparison.
    tree = hashlib.sha256()
    for e in files:
        tree.update(e["path"].encode("utf-8"))
        tree.update(b"\0")
        tree.update(e["sha256"].encode("utf-8"))
        tree.update(b"\n")

    snapshot = {
        "snapshot_version": 1,
        "kind": "odisena-console-static-pwa",
        "deploy": {
            "type": "static",
            "build_step": False,
            "hosts": ["vercel", "netlify", "cloudflare-pages", "s3+cloudfront"],
            "reversible": True,
            "note": "No build/server; rollback = re-promote the previous host deploy (see DEPLOYMENT.md).",
        },
        "service_worker_cache": sw_cache_key(),
        "config_files": files,
        "missing_config_files": missing,
        "file_count": len(files),
        "integrity": {"algorithm": "sha256", "config_tree_sha256": tree.hexdigest()},
    }

    print(json.dumps(snapshot, indent=2, sort_keys=True))

    if missing:
        print(f"avpt-state-snapshot: FAIL (missing config files: {missing})", file=sys.stderr)
        return 1
    print(
        f"avpt-state-snapshot: OK ({len(files)} config files, "
        f"cache={snapshot['service_worker_cache']})",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
