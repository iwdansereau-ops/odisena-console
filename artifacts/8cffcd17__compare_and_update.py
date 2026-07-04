#!/usr/bin/env python3
"""compare_and_update.py — evaluate this week's benchmark against the
rolling baseline, then update the baseline.

Runs after run_benchmark.sh has produced benchmarks/reports/latest.json.

Steps:
  1. Load the latest run report.
  2. Load benchmarks/baseline/baseline.json (may be missing on first run).
  3. Compute % deviation for four headline metrics:
       - mean_cpu_pct        (higher is worse)
       - peak_rss_bytes      (higher is worse)
       - p95_export_latency  (higher is worse)
       - mean_throughput_sps (LOWER is worse; we flag drops)
  4. If |deviation| > THRESHOLD_PCT for any regression-direction change, write
     benchmarks/reports/regressions.json summarizing what tripped. This file
     is what the Slack watcher looks for. If nothing regressed, write an
     empty {"regressions": []} so downstream logic is deterministic.
  5. Recompute the baseline as the rolling mean of the last N runs in
     benchmarks/history/ (default N=4 = ~1 month) and write
     benchmarks/baseline/baseline.json.

All paths are relative to the repo root; the script is intended to be invoked
from that directory (the workflow does so).
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import statistics
import sys
from pathlib import Path

# Metrics that regress when they INCREASE.
INCREASE_IS_REGRESSION = ("mean_cpu_pct", "peak_rss_bytes", "p95_export_latency_ms")
# Metrics that regress when they DECREASE.
DECREASE_IS_REGRESSION = ("mean_throughput_sps",)


def extract_metrics(report: dict) -> dict:
    """Flatten a run report into the four comparable scalars."""
    return {
        "mean_cpu_pct":         safe(report, ["process", "cpu_pct",  "mean"]),
        "peak_rss_bytes":       safe(report, ["process", "rss_bytes", "peak"]),
        "p95_export_latency_ms": safe(report, ["latency", "p95_ms"]),
        "mean_throughput_sps":  safe(report, ["throughput_sps"]),
    }


def safe(d: dict, path: list[str]):
    cur = d
    for k in path:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(k)
    return cur


def pct_change(new: float, base: float) -> float | None:
    if base is None or new is None or base == 0:
        return None
    return ((new - base) / base) * 100.0


def rolling_baseline(history_dir: Path, window: int) -> dict:
    """Compute per-metric mean over the most recent <window> history files."""
    files = sorted(glob.glob(str(history_dir / "*.json")))
    files = files[-window:]
    if not files:
        return {"window_size": 0, "runs": [], "metrics": {}}

    samples: dict[str, list[float]] = {
        "mean_cpu_pct": [], "peak_rss_bytes": [],
        "p95_export_latency_ms": [], "mean_throughput_sps": [],
    }
    runs_meta = []
    for f in files:
        with open(f) as fh:
            r = json.load(fh)
        m = extract_metrics(r)
        for k, v in m.items():
            if v is not None:
                samples[k].append(float(v))
        runs_meta.append({"file": os.path.basename(f), "sha": r.get("sha")})

    metrics = {
        k: (statistics.fmean(v) if v else None) for k, v in samples.items()
    }
    return {"window_size": len(files), "runs": runs_meta, "metrics": metrics}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--report",   default="benchmarks/reports/latest.json")
    ap.add_argument("--baseline", default="benchmarks/baseline/baseline.json")
    ap.add_argument("--history",  default="benchmarks/history")
    ap.add_argument("--out",      default="benchmarks/reports/regressions.json")
    ap.add_argument("--threshold-pct", type=float, default=10.0)
    ap.add_argument("--window",       type=int,   default=4,
                    help="Number of most-recent runs used for the rolling baseline.")
    args = ap.parse_args()

    report_path   = Path(args.report)
    baseline_path = Path(args.baseline)
    history_dir   = Path(args.history)
    out_path      = Path(args.out)

    if not report_path.exists():
        print(f"::error::report not found: {report_path}", file=sys.stderr)
        return 2

    with open(report_path) as f:
        report = json.load(f)
    current = extract_metrics(report)

    # ------------------------------------------------------------------ compare
    regressions = []
    have_baseline = baseline_path.exists()
    baseline_metrics: dict = {}

    if have_baseline:
        with open(baseline_path) as f:
            baseline_metrics = json.load(f).get("metrics", {})

        for m in INCREASE_IS_REGRESSION:
            new, base = current.get(m), baseline_metrics.get(m)
            delta = pct_change(new, base)
            if delta is not None and delta > args.threshold_pct:
                regressions.append({
                    "metric": m,
                    "direction": "increase",
                    "current": new,
                    "baseline": base,
                    "pct_change": delta,
                    "threshold_pct": args.threshold_pct,
                })

        for m in DECREASE_IS_REGRESSION:
            new, base = current.get(m), baseline_metrics.get(m)
            delta = pct_change(new, base)
            # Throughput regresses when it drops — i.e. delta is more negative than -threshold.
            if delta is not None and delta < -args.threshold_pct:
                regressions.append({
                    "metric": m,
                    "direction": "decrease",
                    "current": new,
                    "baseline": base,
                    "pct_change": delta,
                    "threshold_pct": args.threshold_pct,
                })

    out_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "recorded_at":    report.get("recorded_at"),
        "sha":            report.get("sha"),
        "run_url":        report.get("run_url"),
        "threshold_pct":  args.threshold_pct,
        "have_baseline":  have_baseline,
        "current":        current,
        "baseline":       baseline_metrics,
        "regressions":    regressions,
    }
    with open(out_path, "w") as f:
        json.dump(payload, f, indent=2)
    print(f"wrote {out_path} ({len(regressions)} regression(s))")

    # -------------------------------------------------------- update baseline
    # Always rebuild from history so the rolling window follows a fixed rule
    # rather than accumulating stale state.
    new_baseline = rolling_baseline(history_dir, args.window)
    new_baseline["threshold_pct"] = args.threshold_pct
    new_baseline["updated_at"] = report.get("recorded_at")
    baseline_path.parent.mkdir(parents=True, exist_ok=True)
    with open(baseline_path, "w") as f:
        json.dump(new_baseline, f, indent=2)
    print(f"wrote {baseline_path} (window={new_baseline['window_size']})")

    # Exit 0 either way — the workflow decides how to react (comment/commit).
    return 0


if __name__ == "__main__":
    sys.exit(main())
