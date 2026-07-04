#!/usr/bin/env python3
"""
evaluate_leak.py — read every diff_*.json produced by `gomem report`,
identify the widest inuse_space regression, and emit:

  1. A machine-readable JSON blob to stdout (consumed by later workflow steps).
  2. A Markdown PR comment body at --out-comment.
  3. A GitHub Actions step summary appended to --out-summary.

Regression rule: any single function whose flat inuse_space delta exceeds
--threshold-bytes (default 500 KB = 512000 B) in the *last* diff in
chronological order is flagged. The last diff is the "first snapshot →
last snapshot" comparison, i.e. the full 15-minute window.
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys
from typing import Any


def human_bytes(n: int) -> str:
    neg = "-" if n < 0 else ""
    v = abs(n)
    KB, MB, GB = 1024, 1024 * 1024, 1024 * 1024 * 1024
    if v >= GB:
        return f"{neg}{v/GB:.2f} GB"
    if v >= MB:
        return f"{neg}{v/MB:.2f} MB"
    if v >= KB:
        return f"{neg}{v/KB:.1f} KB"
    return f"{neg}{v} B"


def load_reports(reports_dir: pathlib.Path) -> list[dict[str, Any]]:
    files = sorted(reports_dir.glob("diff_*.json"))
    out = []
    for f in files:
        try:
            out.append(json.loads(f.read_text()))
        except Exception as e:  # noqa: BLE001
            print(f"warning: failed to parse {f}: {e}", file=sys.stderr)
    return out


def build_full_window_diff(reports: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Return a synthesized "start → end" diff.

    We already have per-adjacent-pair diffs in chronological order. Rather
    than re-running `gomem diff` on the outer pair, we synthesize the
    equivalent by summing per-function flat/cum deltas across every
    intermediate diff. This is correct because deltas are additive over
    disjoint intervals for the same function on a monotonic inuse_space
    signal (and remains a good proxy even when GC reclaims some bytes in
    between — a function that consistently grows will still surface).

    Falls back to the last individual diff if only one is available.
    """
    if not reports:
        return None
    if len(reports) == 1:
        return reports[0]

    totals_delta = 0
    totals_before = reports[0].get("total_inuse_before_bytes", 0)
    totals_after = reports[-1].get("total_inuse_after_bytes", 0)
    for r in reports:
        totals_delta += int(r.get("total_inuse_delta_bytes", 0))

    # Aggregate top_functions across every diff.
    agg: dict[str, dict[str, Any]] = {}
    for r in reports:
        for fn in r.get("top_functions", []):
            key = fn["function"]
            slot = agg.setdefault(
                key,
                {
                    "function": key,
                    "file": fn.get("file", ""),
                    "line": fn.get("line", 0),
                    "flat_delta": 0,
                    "cum_delta": 0,
                },
            )
            slot["flat_delta"] += int(fn.get("flat_delta", 0))
            slot["cum_delta"] += int(fn.get("cum_delta", 0))
            # Prefer a non-empty source ref.
            if not slot["file"] and fn.get("file"):
                slot["file"] = fn["file"]
                slot["line"] = fn.get("line", 0)

    top = sorted(agg.values(), key=lambda f: f["flat_delta"], reverse=True)
    return {
        "generated_at": reports[-1].get("generated_at"),
        "base_file": reports[0].get("base_file"),
        "current_file": reports[-1].get("current_file"),
        "total_inuse_before_bytes": totals_before,
        "total_inuse_after_bytes": totals_after,
        "total_inuse_delta_bytes": totals_delta,
        "top_functions": top[:5],
    }


def trim_source(path: str) -> str:
    if not path:
        return ""
    parts = path.split("/")
    if len(parts) <= 3:
        return path
    return ".../" + "/".join(parts[-3:])


def render_comment(
    window: dict[str, Any],
    threshold: int,
    sha: str,
    short_sha: str,
    run_url: str,
    has_regression: bool,
) -> str:
    top = window.get("top_functions", [])[:5]
    total = int(window.get("total_inuse_delta_bytes", 0))

    if has_regression:
        header = (
            f"### 🚨 Staging memory regression detected on `{short_sha}`\n\n"
            f"One or more functions retained more than **{human_bytes(threshold)}** "
            f"of `inuse_space` between the first and last heap snapshot "
            f"(15-minute window)."
        )
    elif top:
        header = (
            f"### ✅ Staging memory check passed on `{short_sha}`\n\n"
            f"Largest per-function growth over the 15-minute window stayed under the "
            f"**{human_bytes(threshold)}** threshold. Top offenders listed for reference."
        )
    else:
        header = (
            f"### ✅ Staging memory check passed on `{short_sha}`\n\n"
            f"No positive `inuse_space` deltas detected across 5 snapshots."
        )

    lines: list[str] = [
        "<!-- gomem-staging-memory-check -->",
        header,
        "",
        f"- **Total `inuse_space` delta:** {human_bytes(total)}",
        f"- **Snapshots:** 5 over 15 min",
        f"- **Threshold per function:** {human_bytes(threshold)} (`flat_delta`)",
        f"- **Deployed commit:** [`{short_sha}`](../commit/{sha})",
        f"- **Full report + SVG call graph:** [workflow run]({run_url}) "
        f"(download the `gomem-staging-{short_sha}` artifact)",
        "",
    ]

    if top:
        lines += [
            "#### Top 5 functions by retained bytes",
            "",
            "| # | Function | Flat Δ | Cum Δ | Source |",
            "|--:|----------|-------:|------:|--------|",
        ]
        for i, fn in enumerate(top, 1):
            flat = int(fn.get("flat_delta", 0))
            cum = int(fn.get("cum_delta", 0))
            src = trim_source(fn.get("file", ""))
            line_no = fn.get("line", 0)
            src_cell = f"`{src}:{line_no}`" if src else "—"
            over = " 🚨" if flat > threshold else ""
            lines.append(
                f"| {i}{over} | `{fn['function']}` | "
                f"{human_bytes(flat)} | {human_bytes(cum)} | {src_cell} |"
            )
        lines.append("")

    if has_regression:
        offenders = [f for f in top if int(f.get("flat_delta", 0)) > threshold]
        lines += ["#### Suggested next steps", ""]
        for fn in offenders:
            src = trim_source(fn.get("file", ""))
            line_no = fn.get("line", 0)
            src_ref = f"`{src}:{line_no}`" if src else "the function above"
            lines.append(f"- Inspect {src_ref} for one of the usual culprits:")
            lines.append(
                "  unbounded slice/map appends · missing cache eviction · "
                "goroutine blocked on channel · `sync.Pool` retention · "
                "unclosed response body / rows / file handle."
            )
        lines += [
            "",
            "Reproduce locally against the same commit:",
            "",
            "```bash",
            f"git checkout {sha}",
            "go build -o bin/gomem ./cmd/gomem",
            "./scripts/staging-capture.sh $STAGING_PPROF_URL 180 5",
            "./bin/gomem serve --dir ./profiles --reports ./reports",
            "```",
            "",
        ]

    lines.append(
        "_This comment is updated in place by the `staging-memory-check` "
        "workflow after every successful staging deploy._"
    )
    return "\n".join(lines) + "\n"


def render_summary(comment_md: str) -> str:
    # GitHub step summaries render Markdown, so we can reuse the PR body.
    return "## Staging memory check\n\n" + comment_md


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--reports-dir", required=True, type=pathlib.Path)
    ap.add_argument("--threshold-bytes", required=True, type=int)
    ap.add_argument("--sha", required=True)
    ap.add_argument("--short-sha", required=True)
    ap.add_argument("--run-url", required=True)
    ap.add_argument("--out-comment", required=True, type=pathlib.Path)
    ap.add_argument("--out-summary", required=True, type=pathlib.Path)
    args = ap.parse_args()

    reports = load_reports(args.reports_dir)
    if not reports:
        result = {
            "has_regression": False,
            "worst_bytes": 0,
            "worst_function": "",
            "total_delta_bytes": 0,
            "note": "no reports generated",
        }
        print(json.dumps(result))
        args.out_comment.write_text(
            "<!-- gomem-staging-memory-check -->\n"
            f"### ⚠️ Staging memory check inconclusive on `{args.short_sha}`\n\n"
            "No diff reports were produced — check the workflow logs.\n"
        )
        # step summary is optional; append only if writable.
        try:
            with open(args.out_summary, "a") as f:
                f.write(render_summary(args.out_comment.read_text()))
        except OSError:
            pass
        return 0

    window = build_full_window_diff(reports)
    top = window.get("top_functions", [])
    worst = top[0] if top else {"function": "", "flat_delta": 0}
    worst_bytes = int(worst.get("flat_delta", 0))
    has_regression = worst_bytes > args.threshold_bytes

    comment = render_comment(
        window=window,
        threshold=args.threshold_bytes,
        sha=args.sha,
        short_sha=args.short_sha,
        run_url=args.run_url,
        has_regression=has_regression,
    )
    args.out_comment.write_text(comment)

    # Step summary
    try:
        with open(args.out_summary, "a") as f:
            f.write(render_summary(comment))
    except OSError:
        pass

    result = {
        "has_regression": has_regression,
        "worst_bytes": worst_bytes,
        "worst_function": worst.get("function", ""),
        "total_delta_bytes": int(window.get("total_inuse_delta_bytes", 0)),
        "top_count": len(top),
    }
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
