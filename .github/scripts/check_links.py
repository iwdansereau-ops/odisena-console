#!/usr/bin/env python3
"""link-check: verify local references resolve to real files. No network.

Sources of references checked:
  - catalog.json    -> every runbooks[].path and artifacts[].path
  - sw.js           -> every entry in the ASSETS precache list
  - manifest.webmanifest -> every icons[].src

A reference resolves if the file exists in the working tree OR is tracked in
the git HEAD tree. The git fallback keeps the check correct in a sparse
checkout (where blobs may not be materialized on disk) while still catching
genuinely dangling references. Only local, relative references are checked;
external URLs are never fetched, so the check is deterministic and offline.
"""
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def tracked_files() -> set[str]:
    try:
        out = subprocess.run(
            ["git", "-C", str(ROOT), "ls-tree", "-r", "--name-only", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout
        return set(out.splitlines())
    except (subprocess.CalledProcessError, FileNotFoundError):
        return set()


def normalize(ref: str) -> str | None:
    ref = ref.strip()
    if not ref or ref.startswith(("#", "data:", "http://", "https://", "//", "mailto:")):
        return None
    if ref.startswith("./"):
        ref = ref[2:]
    if ref in ("", "./"):
        ref = "index.html"
    return ref.split("?", 1)[0].split("#", 1)[0]


def sw_assets(text: str) -> list[str]:
    m = re.search(r"const\s+ASSETS\s*=\s*\[(.*?)\]", text, re.DOTALL)
    if not m:
        return []
    return re.findall(r"""['"]([^'"]+)['"]""", m.group(1))


def collect_refs() -> list[tuple[str, str]]:
    """Return (source_label, reference_path) pairs."""
    refs: list[tuple[str, str]] = []

    catalog = json.loads((ROOT / "catalog.json").read_text(encoding="utf-8"))
    for coll in ("runbooks", "artifacts"):
        for item in catalog.get(coll, []):
            p = item.get("path")
            if isinstance(p, str):
                refs.append((f"catalog.json:{coll}", p))

    sw = ROOT / "sw.js"
    if sw.is_file():
        for a in sw_assets(sw.read_text(encoding="utf-8")):
            n = normalize(a)
            if n:
                refs.append(("sw.js:ASSETS", n))

    manifest = ROOT / "manifest.webmanifest"
    if manifest.is_file():
        data = json.loads(manifest.read_text(encoding="utf-8"))
        for icon in data.get("icons", []):
            src = icon.get("src")
            n = normalize(src) if isinstance(src, str) else None
            if n:
                refs.append(("manifest.webmanifest:icons", n))

    return refs


def main() -> int:
    tracked = tracked_files()
    refs = collect_refs()
    missing: list[tuple[str, str]] = []
    for label, ref in refs:
        norm = normalize(ref) or ref
        on_disk = (ROOT / norm).is_file()
        in_git = norm in tracked
        if not (on_disk or in_git):
            missing.append((label, ref))

    checked = len(refs)
    if missing:
        print(f"link-check: FAIL ({len(missing)}/{checked} references unresolved)")
        for label, ref in missing:
            print(f"  - {ref}  (from {label})")
        return 1
    print(f"link-check: OK ({checked} local references resolved)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
