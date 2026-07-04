#!/usr/bin/env python3
"""Aggregate gotestsum JUnit XML reports into a Markdown PR comment.

Called by the `pr-summary` job of .github/workflows/integration-tests.yml
after `actions/download-artifact` has placed every matrix leg's reports into
`--input-dir` (one subdirectory per artifact, e.g. `test-reports-1_23_x/`).

The output is written to `--output` (default: stdout) and is small enough to
fit into a single sticky PR comment. It surfaces:

  * a per-Go-version pass/fail table (plain suite + `-race` suite),
  * TestTelemetry's status called out specifically (that's the OTLP-schema
    canary),
  * up to N failing test names with a short excerpt of the failure message,
  * a link back to the workflow run for full logs and artifacts.

Standard-library only — no pip installs in the workflow.
"""

from __future__ import annotations

import argparse
import glob
import os
import re
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Optional

# Cap the amount of failure detail we inline so we never exceed GitHub's
# 65,536-character comment limit even under a full matrix meltdown.
MAX_FAILURES_INLINE = 10
MAX_FAILURE_CHARS = 500

# Filenames that gotestsum writes look like:
#   junit-1_23_x.xml           -> plain suite,  go = 1.23.x
#   junit-race-1_23_x.xml      -> race suite,   go = 1.23.x
FILENAME_RE = re.compile(r"^junit(?P<race>-race)?-(?P<slug>.+)\.xml$")


@dataclass
class TestFailure:
    suite: str
    name: str
    message: str


@dataclass
class LegResult:
    go_version: str
    plain_total: int = 0
    plain_failed: int = 0
    plain_skipped: int = 0
    plain_present: bool = False
    race_total: int = 0
    race_failed: int = 0
    race_skipped: int = 0
    race_present: bool = False
    test_telemetry_plain: Optional[str] = None  # "pass" | "fail" | "skip" | None
    test_telemetry_race: Optional[str] = None
    failures: list[TestFailure] = field(default_factory=list)


def slug_to_go_version(slug: str) -> str:
    """Turn "1_23_x" back into "1.23.x"."""
    return slug.replace("_", ".")


def parse_junit(path: str) -> tuple[int, int, int, list[TestFailure], Optional[str]]:
    """Return (total, failed, skipped, failures, test_telemetry_status)."""
    total = failed = skipped = 0
    failures: list[TestFailure] = []
    telemetry_status: Optional[str] = None

    try:
        tree = ET.parse(path)
    except ET.ParseError as exc:
        # A malformed report is itself a failure signal.
        return 0, 1, 0, [
            TestFailure(suite="(junit-parser)", name=os.path.basename(path),
                        message=f"Could not parse JUnit XML: {exc}")
        ], None

    root = tree.getroot()
    # gotestsum produces either <testsuites> or a bare <testsuite>.
    suites = root.findall("testsuite") if root.tag == "testsuites" else [root]

    for suite in suites:
        suite_name = suite.attrib.get("name", "")
        for tc in suite.findall("testcase"):
            total += 1
            name = tc.attrib.get("name", "")
            failure_el = tc.find("failure")
            error_el = tc.find("error")
            skipped_el = tc.find("skipped")

            status: str
            if failure_el is not None or error_el is not None:
                failed += 1
                status = "fail"
                el = failure_el if failure_el is not None else error_el
                # message attr is short; text body has the stack.
                raw = (el.attrib.get("message") or el.text or "").strip()
                if len(raw) > MAX_FAILURE_CHARS:
                    raw = raw[:MAX_FAILURE_CHARS] + "…"
                failures.append(TestFailure(suite=suite_name, name=name, message=raw))
            elif skipped_el is not None:
                skipped += 1
                status = "skip"
            else:
                status = "pass"

            # The OTLP-schema canary. Match either the leaf name or a fully-
            # qualified variant produced by subtests.
            if name == "TestTelemetry" or name.startswith("TestTelemetry/"):
                # Preserve the "worst" observation if there are subtests.
                priority = {"fail": 3, "skip": 2, "pass": 1, None: 0}
                if priority[status] > priority[telemetry_status]:
                    telemetry_status = status

    return total, failed, skipped, failures, telemetry_status


def collect(input_dir: str) -> list[LegResult]:
    """Walk every artifact subdir and merge plain + race reports per go version."""
    legs: dict[str, LegResult] = {}

    for xml_path in sorted(glob.glob(os.path.join(input_dir, "**", "*.xml"),
                                     recursive=True)):
        fname = os.path.basename(xml_path)
        m = FILENAME_RE.match(fname)
        if not m:
            continue

        is_race = m.group("race") is not None
        slug = m.group("slug")
        go_version = slug_to_go_version(slug)
        leg = legs.setdefault(go_version, LegResult(go_version=go_version))

        total, failed, skipped, failures, telemetry_status = parse_junit(xml_path)

        if is_race:
            leg.race_present = True
            leg.race_total = total
            leg.race_failed = failed
            leg.race_skipped = skipped
            leg.test_telemetry_race = telemetry_status
        else:
            leg.plain_present = True
            leg.plain_total = total
            leg.plain_failed = failed
            leg.plain_skipped = skipped
            leg.test_telemetry_plain = telemetry_status

        # Tag suite failures with the lane so the reader knows which one broke.
        lane = "race" if is_race else "plain"
        for f in failures:
            f.suite = f"go {go_version} · {lane}"
        leg.failures.extend(failures)

    return sorted(legs.values(), key=lambda leg: leg.go_version)


def status_cell(present: bool, failed: int, total: int) -> str:
    if not present:
        return "⏭️ n/a"
    if failed == 0 and total > 0:
        return f"✅ {total} passed"
    if total == 0:
        return "⚠️ no tests"
    return f"❌ {failed}/{total} failed"


def telemetry_cell(status: Optional[str]) -> str:
    if status == "pass":
        return "✅ pass"
    if status == "fail":
        return "❌ **fail**"
    if status == "skip":
        return "⏭️ skip"
    return "— not run"


def render(legs: list[LegResult], repo: str, run_id: str, sha: str) -> str:
    run_url = f"https://github.com/{repo}/actions/runs/{run_id}"
    lines: list[str] = []

    total_failed = sum(l.plain_failed + l.race_failed for l in legs)
    any_telemetry_fail = any(
        (l.test_telemetry_plain == "fail" or l.test_telemetry_race == "fail")
        for l in legs
    )

    if not legs:
        lines.append("### 🟡 OTLP integration tests — no reports found")
        lines.append("")
        lines.append(
            "The matrix jobs did not upload any JUnit reports. Check the "
            f"[workflow run]({run_url}) for setup failures."
        )
        return "\n".join(lines) + "\n"

    if total_failed == 0:
        headline = "### ✅ OTLP integration tests — all green"
    elif any_telemetry_fail:
        headline = "### ❌ OTLP integration tests — `TestTelemetry` failed"
    else:
        headline = "### ❌ OTLP integration tests — failures detected"

    lines.append(headline)
    lines.append("")
    lines.append(f"Commit `{sha[:7]}` · [full logs and artifacts]({run_url})")
    lines.append("")

    # Per-leg summary table.
    lines.append("| Go version | Suite | `-race` | `TestTelemetry` (plain) | `TestTelemetry` (race) |")
    lines.append("|------------|-------|---------|-------------------------|------------------------|")
    for leg in legs:
        lines.append(
            f"| `{leg.go_version}` "
            f"| {status_cell(leg.plain_present, leg.plain_failed, leg.plain_total)} "
            f"| {status_cell(leg.race_present, leg.race_failed, leg.race_total)} "
            f"| {telemetry_cell(leg.test_telemetry_plain)} "
            f"| {telemetry_cell(leg.test_telemetry_race)} |"
        )
    lines.append("")

    # Highlight the OTLP-schema canary explicitly. This is the whole point
    # of the workflow: any breaking OTLP protobuf/attribute change surfaces
    # as a TestTelemetry failure on every required Go version at once.
    if any_telemetry_fail:
        lines.append(
            "> ⚠️ **`TestTelemetry` is the OTLP-schema canary.** A failure "
            "here means the pdata → OTLP protobuf → pdata round-trip no "
            "longer matches, or an attribute set was dropped or renamed. "
            "**Do not merge** until this is understood."
        )
        lines.append("")

    # Failure detail (bounded).
    all_failures: list[TestFailure] = []
    for leg in legs:
        all_failures.extend(leg.failures)

    if all_failures:
        lines.append("<details>")
        shown = min(len(all_failures), MAX_FAILURES_INLINE)
        lines.append(
            f"<summary>Failing tests ({shown} of {len(all_failures)} shown)</summary>"
        )
        lines.append("")
        for f in all_failures[:MAX_FAILURES_INLINE]:
            lines.append(f"**`{f.name}`** — {f.suite}")
            lines.append("")
            lines.append("```")
            lines.append(f.message or "(no failure message captured)")
            lines.append("```")
            lines.append("")
        if len(all_failures) > MAX_FAILURES_INLINE:
            lines.append(
                f"…and {len(all_failures) - MAX_FAILURES_INLINE} more. "
                f"See the [workflow run]({run_url}) for the full list."
            )
        lines.append("</details>")
        lines.append("")

    lines.append(
        "<sub>Posted by `.github/workflows/integration-tests.yml` · "
        "sticky comment key: `otlp-integration-tests`</sub>"
    )
    return "\n".join(lines) + "\n"


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input-dir", required=True)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--sha", required=True)
    parser.add_argument("--output", default="-")
    args = parser.parse_args(argv)

    legs = collect(args.input_dir)
    body = render(legs, args.repo, args.run_id, args.sha)

    if args.output == "-":
        sys.stdout.write(body)
    else:
        with open(args.output, "w", encoding="utf-8") as fh:
            fh.write(body)

    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
