#!/usr/bin/env python3
"""Focused tests for sw-cache-bump decision logic and parsers.

Runs without git, network, or a test framework. Exits non-zero on failure.
"""
import sys

from check_sw_cache_bump import evaluate, extract_assets, extract_cache

ASSETS = ["index.html", "styles.css", "app.js", "catalog.json",
          "icons/icon-192.png"]

CASES = [
    # (name, changed, base_cache, head_cache, expect_ok)
    ("no controlled asset changed -> pass",
     ["README.md", "DEPLOYMENT.md"], "odisena-v2", "odisena-v2", True),
    ("asset changed, cache NOT bumped -> fail",
     ["app.js"], "odisena-v2", "odisena-v2", False),
    ("asset changed, cache bumped -> pass",
     ["app.js"], "odisena-v2", "odisena-v3", True),
    ("index via './' controlled, changed, no bump -> fail",
     ["index.html", "docs/x.md"], "odisena-v2", "odisena-v2", False),
    ("new sw.js (no base cache) -> pass",
     ["app.js"], None, "odisena-v2", True),
    ("head sw.js missing CACHE decl -> fail",
     ["styles.css"], "odisena-v2", None, False),
    ("icon changed, no bump -> fail",
     ["icons/icon-192.png"], "odisena-v1", "odisena-v1", False),
    ("only sw.js logic changed (not in ASSETS) -> pass",
     ["sw.js"], "odisena-v2", "odisena-v2", True),
]


def run() -> int:
    failures = 0
    for name, changed, base_c, head_c, expect in CASES:
        ok, msg = evaluate(changed, ASSETS, base_c, head_c)
        status = "PASS" if ok == expect else "FAIL"
        if ok != expect:
            failures += 1
        print(f"  [{status}] {name}: got ok={ok} ({msg})")

    sample_sw = (
        "const CACHE = 'odisena-v9';\n"
        "const ASSETS = [\n"
        "  './',\n  './index.html',\n  './icons/favicon.svg',\n];\n"
    )
    assert extract_cache(sample_sw) == "odisena-v9", "extract_cache failed"
    assets = extract_assets(sample_sw)
    assert assets == ["index.html", "index.html", "icons/favicon.svg"], assets
    assert extract_cache("no cache here") is None
    print("  [PASS] parser: extract_cache / extract_assets")

    if failures:
        print(f"test_sw_cache_bump: {failures} FAILURE(S)")
        return 1
    print("test_sw_cache_bump: all cases passed")
    return 0


if __name__ == "__main__":
    sys.exit(run())
