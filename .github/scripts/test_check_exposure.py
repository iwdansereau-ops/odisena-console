#!/usr/bin/env python3
"""Focused tests for the exposure-scan decision core (scan_text).

Runs without network or a test framework. Exits non-zero on failure.
"""
import sys

from check_exposure import scan_text

# (name, text, expect_pattern_names)  — empty set means "must be clean".
CASES = [
    ("personal gmail flagged",
     "DIGEST_TO — your email (`i.w.dansereau@gmail.com`)",
     {"personal-freemail-address"}),
    ("role alias on example.com is allowed",
     "e.g. a team/role alias like `alerts@example.com`",
     set()),
    ("notion api base is allowed (tooling)",
     'NOTION_API_BASE = "https://api.notion.com/v1"',
     set()),
    ("notion share link flagged",
     "url: 'https://app.notion.com/p/39cc43ec8bdd8188a8a0c392ccb2ad86'",
     {"notion-workspace-link"}),
    ("notion.so page link with 32-hex id flagged",
     "see https://www.notion.so/Some-Page-39cc43ec8bdd8188a8a0c392ccb2ad86",
     {"notion-workspace-link"}),
    ("notion.so generic setup page is allowed",
     "1. Visit https://www.notion.so/profile/integrations -> New integration.",
     set()),
    ("notion my-integrations page is allowed",
     "Create a token at https://www.notion.so/my-integrations",
     set()),
    ("asana project link flagged",
     "https://app.asana.com/1/1216220648575767/project/1216497028016174",
     {"asana-workspace-link"}),
    ("vercel authority-gap phrase flagged",
     "P0 · Recover Vercel authority — identify the team/account",
     {"vercel-authority-gap"}),
    ("unregistered domain flagged",
     "Distinct from the unregistered WinfieldChronicles.com.",
     {"unregistered-domain-plan"}),
    ("legal governance detail flagged",
     "Founder-controlled repository named in the Schedule A property inventory.",
     {"legal-governance-detail"}),
    ("benign engineering prose is clean",
     "OTel Collector performance work: sharded state cache, OTTL auditor.",
     set()),
    ("regression-gate-strategy filename is clean",
     '"path": "runbooks/531a968c__regression-gate-strategy.md"',
     set()),
    ("creator attribution name is not flagged",
     "Created by Ian Winfield Dansereau.",
     set()),
]


def run() -> int:
    failures = 0
    for name, text, expect in CASES:
        got = {n for (n, _sev, _snip) in scan_text(text)}
        ok = got == expect
        if not ok:
            failures += 1
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}: got={sorted(got)} "
              f"expected={sorted(expect)}")

    if failures:
        print(f"test_check_exposure: {failures} FAILURE(S)")
        return 1
    print("test_check_exposure: all cases passed")
    return 0


if __name__ == "__main__":
    sys.exit(run())
