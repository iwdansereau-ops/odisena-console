#!/usr/bin/env python3
"""
analyze_runner_variance.py
==========================
Ingest the last N days of nightly scorecards, group per-sample measurements
by the self-hosted runner that produced them, compute the coefficient of
variation (CV) for each (runner, metric) cell, and flag runners whose
overall noise is disproportionate to the fleet.

Why per-runner CV matters
-------------------------
Cloud runners are not identical. Even in a homogeneous label pool
("otel-perf-runner"), individual hosts sit on different hypervisors,
different physical racks, and different noisy-neighbor cohorts. A single
noisy node poisons the rolling baseline for every metric it touches,
which then loosens the adaptive threshold and lets real regressions slip.

This tool identifies those nodes so they can be quarantined, reprovisioned,
or excluded from the baseline pool.

Inputs
------
Per-run scorecard JSON must include the fields written by the extended
`compare_to_baseline.py` schema:

    {
      "commit": "abc123",
      "runner": {"name": "perf-runner-07", "labels": ["self-hosted", "bare-metal"]},
      "started_at": "2026-06-29T06:04:11Z",
      "results": [
        {
          "id": "e2e_p99_latency_ms",
          "current": 189.6,
          "samples": [188.2, 191.4, 187.9, 194.0, 189.7],
          ...
        },
        ...
      ]
    }

Outputs
-------
    reports/runner-variance/runner_variance.json   machine-readable
    reports/runner-variance/runner_variance.md     human-readable

Detection logic
---------------
1. Per (runner, metric) — compute CV over ALL samples that runner produced.
2. Per metric — compute the fleet median CV and MAD (median absolute deviation).
3. Flag a (runner, metric) cell as HIGH_NOISE when:
       robust_z = 0.6745 * (cv - median_cv) / mad  >  robust_z_threshold
   (Iglewicz-Hoaglin modified z-score; robust to outliers unlike stdev-z.)
4. Roll up per runner: a runner is "quarantine-candidate" if it is HIGH_NOISE
   on >= quarantine_metric_fraction of tracked metrics.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import statistics
import sys
from collections import defaultdict
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

try:
    import yaml
except ImportError:
    yaml = None  # config is optional; we have sane defaults


# ---------------------------------------------------------------------------
# Defaults (override via --config or CLI flags)
# ---------------------------------------------------------------------------
DEFAULTS = {
    "window_days": 7,
    "min_runs_per_runner": 3,
    "min_samples_per_cell": 10,
    "robust_z_threshold": 3.5,   # Iglewicz & Hoaglin recommended cutoff
    "quarantine_metric_fraction": 0.30,  # >=30% of metrics HIGH_NOISE -> quarantine
    "watch_metric_fraction": 0.15,       # 15-30% -> watchlist
    "fleet_target_cv_pct": 3.0,          # informational; not a gate
}


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------
@dataclass
class Cell:
    runner: str
    metric: str
    n_runs: int
    n_samples: int
    mean: float
    stdev: float
    cv_pct: float
    robust_z: float | None
    verdict: str  # "OK" | "HIGH_NOISE" | "INSUFFICIENT_DATA"


@dataclass
class RunnerRollup:
    runner: str
    total_runs: int
    metrics_tracked: int
    high_noise_metrics: list[str]
    watchlist_metrics: list[str]
    mean_cv_across_metrics: float
    verdict: str  # "OK" | "WATCH" | "QUARANTINE" | "INSUFFICIENT_DATA"
    first_seen: str
    last_seen: str


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def coefficient_of_variation(values: list[float]) -> float:
    if len(values) < 2:
        return 0.0
    m = statistics.fmean(values)
    if m == 0:
        return 0.0
    return statistics.stdev(values) / abs(m) * 100.0


def median_abs_deviation(values: list[float]) -> float:
    if not values:
        return 0.0
    med = statistics.median(values)
    return statistics.median([abs(v - med) for v in values])


def parse_ts(ts: str) -> datetime:
    """Accept RFC-3339 with or without trailing Z."""
    if ts.endswith("Z"):
        ts = ts[:-1] + "+00:00"
    return datetime.fromisoformat(ts)


def load_scorecards(
    input_dir: Path, window_days: int, now: datetime | None = None
) -> list[dict[str, Any]]:
    """Load every scorecard*.json in input_dir whose started_at is within window."""
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=window_days)
    out: list[dict[str, Any]] = []
    for p in sorted(input_dir.rglob("*.json")):
        try:
            doc = json.loads(p.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        if "results" not in doc or "runner" not in doc:
            continue
        started = doc.get("started_at")
        if not started:
            continue
        try:
            if parse_ts(started) < cutoff:
                continue
        except ValueError:
            continue
        doc["_source_file"] = str(p)
        out.append(doc)
    return out


def group_samples(
    scorecards: list[dict[str, Any]],
) -> tuple[
    dict[tuple[str, str], list[float]],   # (runner, metric) -> pooled samples
    dict[str, set[str]],                  # runner -> {run_ids seen}
    dict[str, tuple[str, str]],           # runner -> (first_seen, last_seen)
]:
    pooled: dict[tuple[str, str], list[float]] = defaultdict(list)
    runs_per_runner: dict[str, set[str]] = defaultdict(set)
    seen_range: dict[str, tuple[str, str]] = {}

    for sc in scorecards:
        runner = sc["runner"].get("name") or "unknown"
        run_id = sc.get("commit") or sc.get("_source_file", "")
        runs_per_runner[runner].add(run_id)

        started = sc.get("started_at", "")
        if runner not in seen_range:
            seen_range[runner] = (started, started)
        else:
            lo, hi = seen_range[runner]
            seen_range[runner] = (min(lo, started), max(hi, started))

        for r in sc["results"]:
            samples = r.get("samples")
            if not samples:
                # scorecards may only carry the aggregated `current` value.
                # Fall back to that so we can still compute run-to-run CV.
                if "current" in r:
                    samples = [r["current"]]
                else:
                    continue
            pooled[(runner, r["id"])].extend(float(x) for x in samples)

    return pooled, runs_per_runner, seen_range


# ---------------------------------------------------------------------------
# Analysis
# ---------------------------------------------------------------------------
def analyze(
    pooled: dict[tuple[str, str], list[float]],
    runs_per_runner: dict[str, set[str]],
    seen_range: dict[str, tuple[str, str]],
    cfg: dict[str, Any],
) -> tuple[list[Cell], list[RunnerRollup], dict[str, dict[str, float]]]:

    min_runs = int(cfg["min_runs_per_runner"])
    min_samples = int(cfg["min_samples_per_cell"])
    z_thresh = float(cfg["robust_z_threshold"])

    # 1) Build per-cell records
    cells: list[Cell] = []
    per_metric_cvs: dict[str, list[float]] = defaultdict(list)

    for (runner, metric), samples in pooled.items():
        n_runs = len(runs_per_runner[runner])
        if n_runs < min_runs or len(samples) < min_samples:
            cells.append(Cell(
                runner=runner, metric=metric,
                n_runs=n_runs, n_samples=len(samples),
                mean=statistics.fmean(samples) if samples else 0.0,
                stdev=0.0, cv_pct=0.0, robust_z=None,
                verdict="INSUFFICIENT_DATA",
            ))
            continue
        mean = statistics.fmean(samples)
        stdev = statistics.stdev(samples)
        cv = coefficient_of_variation(samples)
        cells.append(Cell(
            runner=runner, metric=metric,
            n_runs=n_runs, n_samples=len(samples),
            mean=mean, stdev=stdev, cv_pct=cv,
            robust_z=None, verdict="OK",   # provisional; refined below
        ))
        per_metric_cvs[metric].append(cv)

    # 2) Fleet stats per metric (median CV + MAD for robust-z)
    fleet_stats: dict[str, dict[str, float]] = {}
    for metric, cvs in per_metric_cvs.items():
        med = statistics.median(cvs)
        mad = median_abs_deviation(cvs)
        fleet_stats[metric] = {
            "median_cv_pct": med,
            "mad_cv_pct": mad,
            "n_runners": len(cvs),
            "min_cv_pct": min(cvs),
            "max_cv_pct": max(cvs),
        }

    # 3) Score each OK cell against its metric's fleet distribution
    for c in cells:
        if c.verdict != "OK":
            continue
        stats_m = fleet_stats.get(c.metric)
        if not stats_m or stats_m["mad_cv_pct"] == 0:
            # Not enough peers to compare against — leave as OK.
            continue
        # Iglewicz-Hoaglin modified z-score
        rz = 0.6745 * (c.cv_pct - stats_m["median_cv_pct"]) / stats_m["mad_cv_pct"]
        c.robust_z = rz
        if rz > z_thresh:
            c.verdict = "HIGH_NOISE"

    # 4) Roll up per runner
    per_runner: dict[str, list[Cell]] = defaultdict(list)
    for c in cells:
        per_runner[c.runner].append(c)

    rollups: list[RunnerRollup] = []
    for runner, rcells in per_runner.items():
        tracked = [c for c in rcells if c.verdict in ("OK", "HIGH_NOISE")]
        high = [c for c in rcells if c.verdict == "HIGH_NOISE"]
        n_runs = len(runs_per_runner[runner])
        first_seen, last_seen = seen_range.get(runner, ("", ""))

        if n_runs < min_runs or not tracked:
            verdict = "INSUFFICIENT_DATA"
            mean_cv = 0.0
        else:
            frac = len(high) / len(tracked)
            mean_cv = statistics.fmean([c.cv_pct for c in tracked])
            if frac >= cfg["quarantine_metric_fraction"]:
                verdict = "QUARANTINE"
            elif frac >= cfg["watch_metric_fraction"]:
                verdict = "WATCH"
            else:
                verdict = "OK"

        # Sort so highest-noise metrics show up first
        high_sorted = sorted(high, key=lambda c: c.robust_z or 0, reverse=True)
        watch_candidates = [
            c for c in tracked
            if c.robust_z is not None and 2.0 <= c.robust_z <= z_thresh
        ]
        watch_sorted = sorted(watch_candidates, key=lambda c: c.robust_z or 0, reverse=True)

        rollups.append(RunnerRollup(
            runner=runner,
            total_runs=n_runs,
            metrics_tracked=len(tracked),
            high_noise_metrics=[f"{c.metric} (CV={c.cv_pct:.2f}%, z={c.robust_z:.2f})"
                                for c in high_sorted],
            watchlist_metrics=[f"{c.metric} (CV={c.cv_pct:.2f}%, z={c.robust_z:.2f})"
                               for c in watch_sorted],
            mean_cv_across_metrics=mean_cv,
            verdict=verdict,
            first_seen=first_seen,
            last_seen=last_seen,
        ))

    rollups.sort(key=lambda r: (
        {"QUARANTINE": 0, "WATCH": 1, "OK": 2, "INSUFFICIENT_DATA": 3}[r.verdict],
        -r.mean_cv_across_metrics,
    ))
    return cells, rollups, fleet_stats


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------
VERDICT_ICON = {
    "OK": "✅",
    "WATCH": "🟡",
    "QUARANTINE": "🔴",
    "INSUFFICIENT_DATA": "⚪",
    "HIGH_NOISE": "🔴",
}


def write_markdown(
    rollups: list[RunnerRollup],
    cells: list[Cell],
    fleet_stats: dict[str, dict[str, float]],
    cfg: dict[str, Any],
    out: Path,
    window_days: int,
    total_scorecards: int,
) -> None:
    md: list[str] = []
    md.append("# Runner Variance Report")
    md.append("")
    md.append(f"**Window:** last {window_days} days &nbsp;·&nbsp; "
              f"**Scorecards analyzed:** {total_scorecards} &nbsp;·&nbsp; "
              f"**Runners seen:** {len(rollups)}")
    md.append("")

    counts = {"OK": 0, "WATCH": 0, "QUARANTINE": 0, "INSUFFICIENT_DATA": 0}
    for r in rollups:
        counts[r.verdict] += 1
    md.append(
        f"**Summary:** {VERDICT_ICON['QUARANTINE']} {counts['QUARANTINE']} quarantine · "
        f"{VERDICT_ICON['WATCH']} {counts['WATCH']} watch · "
        f"{VERDICT_ICON['OK']} {counts['OK']} healthy · "
        f"{VERDICT_ICON['INSUFFICIENT_DATA']} {counts['INSUFFICIENT_DATA']} insufficient data"
    )
    md.append("")

    # -- Runner rollup ------------------------------------------------------
    md.append("## Runners ranked by noise contribution")
    md.append("")
    md.append("| Runner | Runs | Mean CV | High-noise metrics | Verdict |")
    md.append("|---|---:|---:|---:|:---:|")
    for r in rollups:
        md.append(
            f"| `{r.runner}` | {r.total_runs} | {r.mean_cv_across_metrics:.2f}% | "
            f"{len(r.high_noise_metrics)} / {r.metrics_tracked} | "
            f"{VERDICT_ICON[r.verdict]} {r.verdict} |"
        )
    md.append("")

    # -- Quarantine detail --------------------------------------------------
    quarantine = [r for r in rollups if r.verdict == "QUARANTINE"]
    if quarantine:
        md.append("## 🔴 Quarantine candidates")
        md.append("")
        md.append("Recommend removing these runners from the "
                  "`otel-perf-runner` label until root-caused.")
        md.append("")
        for r in quarantine:
            md.append(f"### `{r.runner}`")
            md.append(f"- Runs in window: **{r.total_runs}** "
                      f"({r.first_seen} → {r.last_seen})")
            md.append(f"- Mean CV across tracked metrics: **{r.mean_cv_across_metrics:.2f}%**")
            md.append(f"- High-noise metrics ({len(r.high_noise_metrics)} of {r.metrics_tracked}):")
            for m in r.high_noise_metrics:
                md.append(f"  - {m}")
            md.append("")
            md.append(_suggested_actions(r))
            md.append("")

    # -- Watchlist ----------------------------------------------------------
    watch = [r for r in rollups if r.verdict == "WATCH"]
    if watch:
        md.append("## 🟡 Watchlist")
        md.append("")
        md.append("Runners producing elevated noise on a minority of metrics. "
                  "Monitor next 7 days; escalate to QUARANTINE if the trend continues.")
        md.append("")
        for r in watch:
            md.append(f"- `{r.runner}` — mean CV {r.mean_cv_across_metrics:.2f}%, "
                      f"high-noise: {', '.join(m.split(' ')[0] for m in r.high_noise_metrics) or 'none'}, "
                      f"borderline: {', '.join(m.split(' ')[0] for m in r.watchlist_metrics[:3])}"
                      + (' …' if len(r.watchlist_metrics) > 3 else ''))
        md.append("")

    # -- Fleet reference ----------------------------------------------------
    md.append("## Fleet CV distribution (reference)")
    md.append("")
    md.append("| Metric | Median CV | MAD | Min | Max | Runners |")
    md.append("|---|---:|---:|---:|---:|---:|")
    for metric, s in sorted(fleet_stats.items()):
        md.append(
            f"| {metric} | {s['median_cv_pct']:.2f}% | {s['mad_cv_pct']:.2f}% | "
            f"{s['min_cv_pct']:.2f}% | {s['max_cv_pct']:.2f}% | {int(s['n_runners'])} |"
        )
    md.append("")

    # -- Methodology --------------------------------------------------------
    md.append("## Methodology")
    md.append("")
    md.append(f"- **CV** = stdev / |mean| × 100 over all samples a runner "
              f"contributed for a given metric during the {window_days}-day window.")
    md.append(f"- **Robust z-score** = 0.6745 × (CV − median_CV) / MAD "
              f"(Iglewicz & Hoaglin). Threshold: **{cfg['robust_z_threshold']}**.")
    md.append(f"- **Quarantine** if a runner is flagged HIGH_NOISE on "
              f"≥ {cfg['quarantine_metric_fraction']:.0%} of its tracked metrics.")
    md.append(f"- **Watch** if HIGH_NOISE fraction is between "
              f"{cfg['watch_metric_fraction']:.0%} and {cfg['quarantine_metric_fraction']:.0%}.")
    md.append(f"- Cells with <{cfg['min_runs_per_runner']} runs or "
              f"<{cfg['min_samples_per_cell']} pooled samples are marked INSUFFICIENT_DATA.")

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(md) + "\n")


def _suggested_actions(r: RunnerRollup) -> str:
    """Static remediation checklist rendered for each quarantine candidate."""
    return (
        "**Suggested remediation checklist:**\n"
        "1. Confirm CPU governor and C-states pinned "
        "(`cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_governor` → `performance`).\n"
        "2. Verify SMT/hyperthreading is disabled "
        "(`lscpu | grep 'Thread(s) per core'` → 1).\n"
        "3. Check NUMA balancing is off "
        "(`cat /proc/sys/kernel/numa_balancing` → 0).\n"
        "4. Inspect for noisy tenants "
        "(`grep -c ^processor /proc/cpuinfo` vs. instance vCPU spec; unexpected steal time in `mpstat 1 5`).\n"
        "5. Reboot and re-run the canary workload from `otel-nightly-perf`; "
        "if canary regresses, the host is the problem, not the code."
    )


def write_json(
    rollups: list[RunnerRollup],
    cells: list[Cell],
    fleet_stats: dict[str, dict[str, float]],
    out: Path,
    window_days: int,
    total_scorecards: int,
) -> None:
    payload = {
        "window_days": window_days,
        "scorecards_analyzed": total_scorecards,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "runners": [asdict(r) for r in rollups],
        "cells": [asdict(c) for c in cells],
        "fleet_stats": fleet_stats,
    }
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, indent=2))


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def load_cfg(path: Path | None) -> dict[str, Any]:
    cfg = dict(DEFAULTS)
    if path and path.exists() and yaml is not None:
        doc = yaml.safe_load(path.read_text()) or {}
        section = doc.get("runner_variance", {})
        for k, v in section.items():
            cfg[k] = v
    return cfg


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Analyze per-runner variance across nightly scorecards."
    )
    ap.add_argument("--input-dir", required=True, type=Path,
                    help="Directory containing scorecard*.json files (recursive).")
    ap.add_argument("--window-days", type=int, default=None,
                    help="How many days of history to include (default: 7).")
    ap.add_argument("--config", type=Path, default=None,
                    help="Optional YAML config with a runner_variance: section.")
    ap.add_argument("--out-json", type=Path,
                    default=Path("reports/runner-variance/runner_variance.json"))
    ap.add_argument("--out-md",   type=Path,
                    default=Path("reports/runner-variance/runner_variance.md"))
    ap.add_argument("--now", type=str, default=None,
                    help="Override current time (RFC-3339) for deterministic tests.")
    ap.add_argument("--fail-on-quarantine", action="store_true",
                    help="Exit non-zero if any runner is QUARANTINE. Useful for CI.")
    args = ap.parse_args()

    cfg = load_cfg(args.config)
    if args.window_days is not None:
        cfg["window_days"] = args.window_days
    window_days = int(cfg["window_days"])

    now = parse_ts(args.now) if args.now else datetime.now(timezone.utc)
    scorecards = load_scorecards(args.input_dir, window_days, now)
    if not scorecards:
        print(f"No scorecards found under {args.input_dir} in the last "
              f"{window_days} days.", file=sys.stderr)
        return 2

    pooled, runs_per_runner, seen_range = group_samples(scorecards)
    cells, rollups, fleet_stats = analyze(pooled, runs_per_runner, seen_range, cfg)

    write_json(rollups, cells, fleet_stats, args.out_json, window_days, len(scorecards))
    write_markdown(rollups, cells, fleet_stats, cfg, args.out_md,
                   window_days, len(scorecards))

    # Console summary
    counts = {"OK": 0, "WATCH": 0, "QUARANTINE": 0, "INSUFFICIENT_DATA": 0}
    for r in rollups:
        counts[r.verdict] += 1
    print(f"Runners: {counts['QUARANTINE']} quarantine, {counts['WATCH']} watch, "
          f"{counts['OK']} ok, {counts['INSUFFICIENT_DATA']} n/a "
          f"({len(scorecards)} scorecards, {window_days}d window)")

    if args.fail_on_quarantine and counts["QUARANTINE"] > 0:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
