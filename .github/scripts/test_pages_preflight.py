#!/usr/bin/env python3
"""Focused tests for the pages-preflight offline decision logic.

Runs without git, network, or a test framework. Exits non-zero on failure.
Mirrors the style of test_sw_cache_bump.py.
"""
import sys

from pages_preflight import EXPECTED_DOMAIN, REQUIRED_FILES, evaluate

ALL = list(REQUIRED_FILES)
GOOD_CNAME = EXPECTED_DOMAIN + "\n"

# (name, cname_raw, present_files, expect_ok)
CASES = [
    ("healthy config -> pass", GOOD_CNAME, ALL, True),
    ("CNAME without trailing newline -> pass", EXPECTED_DOMAIN, ALL, True),
    ("missing CNAME file -> fail", None, [f for f in ALL if f != "CNAME"], False),
    ("missing .nojekyll -> fail", GOOD_CNAME,
     [f for f in ALL if f != ".nojekyll"], False),
    ("missing 404.html -> fail", GOOD_CNAME,
     [f for f in ALL if f != "404.html"], False),
    ("missing index.html -> fail", GOOD_CNAME,
     [f for f in ALL if f != "index.html"], False),
    ("wrong domain -> fail", "example.com\n", ALL, False),
    ("CNAME as URL -> fail", "https://console.odisena.com\n", ALL, False),
    ("CNAME with trailing slash -> fail", "console.odisena.com/\n", ALL, False),
    ("CNAME with surrounding whitespace -> fail",
     "  console.odisena.com  \n", ALL, False),
    ("CNAME with two hostnames -> fail",
     "console.odisena.com\nwww.odisena.com\n", ALL, False),
    ("empty CNAME -> fail", "\n", ALL, False),
]


def run() -> int:
    failures = 0
    for name, cname, present, expect in CASES:
        ok, problems = evaluate(cname, present)
        status = "PASS" if ok == expect else "FAIL"
        if ok != expect:
            failures += 1
        print(f"  [{status}] {name}: got ok={ok} ({'; '.join(problems) or 'no problems'})")

    # A failure set must be non-empty when not ok, and empty when ok.
    ok, problems = evaluate("example.com\n", ALL)
    assert not ok and problems, "failing case must report problems"
    ok, problems = evaluate(GOOD_CNAME, ALL)
    assert ok and not problems, "passing case must report no problems"
    print("  [PASS] problems list mirrors ok flag")

    if failures:
        print(f"test_pages_preflight: {failures} FAILURE(S)")
        return 1
    print("test_pages_preflight: all cases passed")
    return 0


if __name__ == "__main__":
    sys.exit(run())
