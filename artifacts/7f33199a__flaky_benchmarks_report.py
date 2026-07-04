#!/usr/bin/env python3
"""
Weekly flaky-benchmark report.

Reads recent PR comments posted by the benchmark-pr-check workflow,
extracts the "Noise report" tables, aggregates per-metric coefficient of
variation (CV) across the past N days, and emits a Slack-flavored
Markdown summary of the top offenders.

A metric is flagged as flaky when its median CV over the window exceeds
--cv-threshold (default 5%). Median-of-medians is used so a single PR
with a bad run can't drag a stable metric onto the list.

Inputs (either --comments-file or --repo + gh CLI):

  --comments-file : path to a JSON array of {"pr": N, "body": "..."} objects
  --repo OWNER/REPO : fetch comments via `gh` for recently updated PRs

Outputs:
  --output : Markdown report suitable for Slack
"""

from __future__ import annotations

import argparse
import json
import os
import re
import statistics
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path


COMMENT_MARKER = "<!-- benchmark-comparison-marker -->"
NOISE_HEADER = re.compile(r"Noise report.*?across\s+(\d+)\s+runs", re.IGNORECASE)
# Matches: | `metric_name` | 121 | 116.6 | 137.4 | 6.16% |
ROW = re.compile(
    r"\|\s*`([^`]+)`\s*\|\s*([-\d.eE+]+)\s*\|\s*([-\d.eE+]+)\s*\|\s*"
    r"([-\d.eE+]+)\s*\|\s*([-\d.eE+]+)\s*%\s*\|"
)


def parse_noise_tables(body: str) -> list[dict]:
    """Return list of {metric, median, min, max, cv} rows from one comment body."""
    if COMMENT_MARKER not in body or "Noise report" not in body:
        return []
    # Slice from the noise report header to end of body — the table lives there.
    m = NOISE_HEADER.search(body)
    if not m:
        return []
    section = body[m.start():]
    rows: list[dict] = []
    for match in ROW.finditer(section):
        name, med, lo, hi, cv = match.groups()
        try:
            rows.append({
                "metric": name,
                "median": float(med),
                "min": float(lo),
                "max": float(hi),
                "cv": float(cv),
            })
        except ValueError:
            continue
    return rows


def fetch_comments_via_gh(repo: str, since: datetime) -> list[dict]:
    """Use gh CLI to list PRs updated since `since`, then fetch their comments."""
    iso = since.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    # 1. List recently updated PRs (open OR merged).
    prs_raw = subprocess.check_output([
        "gh", "pr", "list", "--repo", repo, "--state", "all",
        "--search", f"updated:>={iso[:10]}",
        "--json", "number,updatedAt", "--limit", "100",
    ], text=True)
    prs = json.loads(prs_raw)
    out: list[dict] = []
    for pr in prs:
        num = pr["number"]
        # 2. For each PR, fetch its comments (issue comments = PR conversation).
        try:
            comments_raw = subprocess.check_output([
                "gh", "api", f"repos/{repo}/issues/{num}/comments",
                "--paginate",
            ], text=True)
        except subprocess.CalledProcessError:
            continue
        for c in json.loads(comments_raw):
            updated = c.get("updated_at") or c.get("created_at") or ""
            body = c.get("body") or ""
            if COMMENT_MARKER not in body:
                continue
            # Filter by recency of the comment itself, not the PR.
            try:
                ts = datetime.fromisoformat(updated.replace("Z", "+00:00"))
            except ValueError:
                continue
            if ts < since:
                continue
            out.append({"pr": num, "body": body, "updated_at": updated})
    return out


def aggregate(comments: list[dict]) -> dict[str, dict]:
    """
    Return {metric: {samples: [cv, ...], prs: {pr_num, ...}, last_median, ...}}.
    """
    agg: dict[str, dict] = {}
    for c in comments:
        rows = parse_noise_tables(c["body"])
        for r in rows:
            entry = agg.setdefault(r["metric"], {
                "cv_samples": [], "median_samples": [],
                "prs": set(), "last_seen": None,
            })
            entry["cv_samples"].append(r["cv"])
            entry["median_samples"].append(r["median"])
            entry["prs"].add(c["pr"])
            ts = c.get("updated_at") or ""
            if ts and (entry["last_seen"] is None or ts > entry["last_seen"]):
                entry["last_seen"] = ts
    # Reduce.
    result: dict[str, dict] = {}
    for name, e in agg.items():
        cvs = e["cv_samples"]
        result[name] = {
            "median_cv": statistics.median(cvs),
            "max_cv": max(cvs),
            "p90_cv": (statistics.quantiles(cvs, n=10)[-1]
                       if len(cvs) >= 10 else max(cvs)),
            "sample_count": len(cvs),
            "pr_count": len(e["prs"]),
            "example_median": statistics.median(e["median_samples"]),
            "last_seen": e["last_seen"],
        }
    return result


def render_report(
    stats: dict[str, dict],
    cv_threshold: float,
    top_n: int,
    window_days: int,
    repo: str | None,
) -> tuple[str, list[str]]:
    """Return (markdown, [flaky_metric_names_ordered])."""
    flaky = [
        (name, s) for name, s in stats.items()
        if s["median_cv"] > cv_threshold and s["sample_count"] >= 2
    ]
    # Rank by median CV descending; break ties by pr_count so widespread flakes win.
    flaky.sort(key=lambda kv: (-kv[1]["median_cv"], -kv[1]["pr_count"]))
    top = flaky[:top_n]

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    lines: list[str] = []
    lines.append(f":bar_chart: *Weekly flaky-benchmark report* — {now}")
    lines.append(f"_Window: last {window_days} days · "
                 f"threshold: CV > {cv_threshold}% · "
                 f"metrics scanned: {len(stats)}_")
    lines.append("")

    if not top:
        lines.append(":white_check_mark: No metrics exceeded the CV threshold this week. "
                     "Benchmark suite is behaving.")
        return "\n".join(lines), []

    lines.append(f"*Top {len(top)} culprit{'s' if len(top) != 1 else ''} "
                 f"(sorted by median CV):*")
    lines.append("")
    for i, (name, s) in enumerate(top, 1):
        lines.append(
            f"{i}. `{name}` — median CV *{s['median_cv']:.2f}%* "
            f"(max {s['max_cv']:.2f}%) across {s['sample_count']} runs "
            f"in {s['pr_count']} PR{'s' if s['pr_count'] != 1 else ''}"
        )

    lines.append("")
    lines.append("*Suggested actions:*")
    lines.append("• Investigate warmup, GC pauses, or I/O in the top metric first.")
    lines.append("• If a metric is inherently noisy, consider moving it to a "
                 "self-hosted runner or excluding it from the regression gate.")
    lines.append("• Metrics with CV > 10% will almost certainly cause false "
                 "positives at the 5% delta threshold.")
    if repo:
        lines.append(f"• Repo: <https://github.com/{repo}/pulls|{repo} PRs>")
    lines.append("")
    lines.append(f"<sub>{len(flaky)} total metric{'s' if len(flaky) != 1 else ''} "
                 f"exceeded the threshold; showing top {len(top)}.</sub>")
    return "\n".join(lines), [name for name, _ in top]


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("--comments-file", type=Path,
                     help="JSON array of {pr, body, updated_at} objects.")
    src.add_argument("--repo",
                     help="OWNER/REPO — fetch via `gh` CLI.")
    p.add_argument("--window-days", type=int, default=7)
    p.add_argument("--cv-threshold", type=float, default=5.0)
    p.add_argument("--top", type=int, default=3)
    p.add_argument("--output", type=Path, required=True)
    args = p.parse_args()

    since = datetime.now(timezone.utc) - timedelta(days=args.window_days)

    if args.comments_file:
        comments = json.loads(args.comments_file.read_text(encoding="utf-8"))
    else:
        comments = fetch_comments_via_gh(args.repo, since)

    if not comments:
        args.output.write_text(
            f":information_source: No benchmark PR comments found in the last "
            f"{args.window_days} days. Nothing to report.\n", encoding="utf-8")
        print("no comments found")
        return 0

    stats = aggregate(comments)
    report, top_names = render_report(
        stats,
        cv_threshold=args.cv_threshold,
        top_n=args.top,
        window_days=args.window_days,
        repo=args.repo,
    )
    args.output.write_text(report, encoding="utf-8")
    print(report)
    # Emit machine-readable summary for downstream Slack step.
    summary_path = args.output.with_suffix(".json")
    summary_path.write_text(json.dumps({
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "window_days": args.window_days,
        "cv_threshold": args.cv_threshold,
        "top_offenders": top_names,
        "metrics": stats,
    }, indent=2, default=str), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
