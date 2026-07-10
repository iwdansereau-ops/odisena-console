#!/usr/bin/env python3
"""sw-cache-bump: require the service-worker CACHE key to change whenever any
service-worker-controlled (precached) asset changes.

The precache list is the ASSETS array in sw.js. If a change set touches any of
those assets but leaves the `const CACHE = '...'` value unchanged, clients would
keep serving stale content — so this check fails and demands a cache-key bump.

Behaviour by event:
  - pull_request : compare the merge-base(base, head)..head range.
  - push         : compare before..head; skipped if there is no real before SHA
                   (new branch / initial push).
  - workflow_dispatch (or missing refs) : informational, always passes.

The decision logic (`evaluate`) is a pure function so it can be unit-tested
without git; see test_sw_cache_bump.py.
"""
import argparse
import re
import subprocess
import sys

ZERO_SHA = "0000000000000000000000000000000000000000"


def extract_cache(text: str) -> str | None:
    m = re.search(r"""const\s+CACHE\s*=\s*['"]([^'"]+)['"]""", text)
    return m.group(1) if m else None


def extract_assets(text: str) -> list[str]:
    m = re.search(r"const\s+ASSETS\s*=\s*\[(.*?)\]", text, re.DOTALL)
    if not m:
        return []
    raw = re.findall(r"""['"]([^'"]+)['"]""", m.group(1))
    out = []
    for a in raw:
        a = a.strip()
        if a.startswith("./"):
            a = a[2:]
        if a in ("", "./"):
            a = "index.html"
        out.append(a)
    return out


def evaluate(changed_files, controlled_assets, base_cache, head_cache):
    """Pure decision. Returns (ok: bool, message: str)."""
    controlled = set(controlled_assets)
    hits = sorted(set(changed_files) & controlled)
    if not hits:
        return True, "no service-worker-controlled assets changed; cache bump not required"
    if base_cache is None:
        return True, "no base sw.js (new file); cache bump not required"
    if head_cache is None:
        return False, "sw.js is missing a `const CACHE = '...'` declaration"
    if base_cache == head_cache:
        return (
            False,
            "controlled assets changed but CACHE was not bumped "
            f"(still {head_cache!r}); changed: {hits}",
        )
    return True, f"CACHE bumped {base_cache!r} -> {head_cache!r}; changed: {hits}"


def git(*args: str) -> str:
    return subprocess.run(["git", *args], capture_output=True, text=True, check=True).stdout


def git_show(ref: str, path: str) -> str | None:
    r = subprocess.run(["git", "show", f"{ref}:{path}"], capture_output=True, text=True)
    return r.stdout if r.returncode == 0 else None


def resolve_base(event: str, base: str, head: str) -> str | None:
    if event == "pull_request":
        r = subprocess.run(["git", "merge-base", base, head], capture_output=True, text=True)
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout.strip()
        return base
    return base


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--event", default="")
    ap.add_argument("--base", default="")
    ap.add_argument("--head", default="")
    args = ap.parse_args()

    base, head = args.base.strip(), args.head.strip()

    if args.event == "workflow_dispatch" or not head:
        print("sw-cache-bump: SKIP (manual/dispatch run, nothing to diff)")
        return 0
    if not base or base == ZERO_SHA:
        print("sw-cache-bump: SKIP (no base ref; new branch or initial push)")
        return 0

    content_base = resolve_base(args.event, base, head)
    if not content_base:
        print("sw-cache-bump: SKIP (could not resolve base)")
        return 0

    try:
        diff = git("diff", "--name-only", f"{content_base}..{head}")
    except subprocess.CalledProcessError as exc:
        print(f"sw-cache-bump: SKIP (git diff failed: {exc.stderr.strip()})")
        return 0
    changed = [line for line in diff.splitlines() if line]

    head_sw = git_show(head, "sw.js")
    if head_sw is None:
        head_sw = (open("sw.js", encoding="utf-8").read()
                   if __import__("pathlib").Path("sw.js").is_file() else "")
    base_sw = git_show(content_base, "sw.js")

    controlled = extract_assets(head_sw)
    head_cache = extract_cache(head_sw)
    base_cache = extract_cache(base_sw) if base_sw is not None else None

    ok, message = evaluate(changed, controlled, base_cache, head_cache)
    print(f"sw-cache-bump: {'OK' if ok else 'FAIL'} — {message}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
