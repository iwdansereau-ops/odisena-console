#!/usr/bin/env python3
"""
check_regression.py
===================
Compare a current benchmark summary against a rolling historical baseline and
flag regressions.

What changed vs. the previous version
-------------------------------------
1. Rolling baseline is now a **windowed median** (was: simple mean of the
   most-recent N files). Median is robust to the single-run p99 spikes and
   throughput drops we see when a GitHub runner gets a noisy neighbor.

2. **MAD-based outlier filtering** is applied to the baseline window before
   the median is taken. Points more than `outlier-mad-k` MADs from the
   window median are dropped. Default k=3.5 (Iglewicz & Hoaglin modified
   Z-score).

3. **Direction-aware comparison.** `latency_ms.p99` regresses when it goes
   *up*; `throughput` regresses when it goes *down*. Metrics are classified
   via `--lower-is-better` (default) or `--higher-is-better`.

4. **Dual schema support.** Works with both:
     a) The `benchmark_viz.py` summary shape:
          {"runs": [{"label": "...", "metrics": {"latency_ms": {"p99": 30.4}}}]}
     b) The flat per-run history shape used by `bench_history.json`:
          {"runs": [{"timestamp": "...", "commit": "...",
                     "throughput": 132372.35,
                     "latency_ms": {"p50": 4.49, "p95": 11.81, "p99": 31.36}}]}
   The metric path is resolved with a dotted key ("latency_ms.p99",
   "throughput"), so the same script works for either.

5. **Warmup guard.** If fewer than `--min-history` post-filter samples remain
   for a series, the run is reported but never flagged as regressed. Prevents
   the first few runs after a schema change from producing false positives.

6. **Deterministic history ordering.** Files are sorted by (timestamp
   embedded in JSON if available, otherwise mtime) so results reproduce
   across machines regardless of filesystem mtime quirks.

Exit codes
----------
0  no regression detected (or insufficient history)
1  regression detected on at least one series
2  usage/input error
"""

from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import Any, Iterable


# --------------------------------------------------------------------------- #
# Data model                                                                  #
# --------------------------------------------------------------------------- #

@dataclass
class RegressionFinding:
    series: str                    # e.g. "gha-ubuntu-22.04::latency_ms.p99"
    metric_path: str
    direction: str                 # "lower_is_better" | "higher_is_better"
    current_value: float
    baseline_median: float
    baseline_mad: float
    baseline_n_used: int
    baseline_n_dropped_as_outlier: int
    delta_abs: float
    delta_rel: float
    regressed: bool
    reason: str
    outliers_dropped: list[float] = field(default_factory=list)


# --------------------------------------------------------------------------- #
# Robust statistics                                                           #
# --------------------------------------------------------------------------- #

def median(xs: Iterable[float]) -> float:
    xs = list(xs)
    if not xs:
        return float("nan")
    return statistics.median(xs)


def mad(xs: list[float], center: float | None = None) -> float:
    """Median Absolute Deviation. Scale-normalized to be a robust stdev
    estimator via 1.4826 (Iglewicz & Hoaglin)."""
    if not xs:
        return float("nan")
    c = center if center is not None else median(xs)
    return 1.4826 * median(abs(x - c) for x in xs)


def filter_outliers(xs: list[float], k: float = 3.5) -> tuple[list[float], list[float]]:
    """Drop points whose modified Z-score exceeds ±k. Returns (kept, dropped).

    Uses MAD-based Z: z_i = (x_i - median) / MAD. When MAD is 0 (all points
    equal), keep everything — nothing is an outlier in a constant series."""
    if len(xs) < 3:
        return list(xs), []
    m = median(xs)
    scale = mad(xs, center=m)
    if scale == 0 or math.isnan(scale):
        return list(xs), []
    kept, dropped = [], []
    for x in xs:
        z = (x - m) / scale
        (dropped if abs(z) > k else kept).append(x)
    return kept, dropped


# --------------------------------------------------------------------------- #
# History loading — supports two schemas                                      #
# --------------------------------------------------------------------------- #

def _get_dotted(obj: dict, path: str) -> Any:
    """Fetch obj[a][b][c] for path 'a.b.c'. Returns None if any hop misses."""
    cur: Any = obj
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


def _extract_points(doc: dict, metric_path: str,
                    series_key: str) -> list[tuple[str, float, str]]:
    """
    Return list of (series_label, value, timestamp) tuples from a single JSON
    document. Handles both schemas.

    series_key controls grouping:
      - "label"     → group by run["label"]           (summary schema)
      - "env"       → group by run["env"]             (flat history schema)
      - "commit"    → one series per commit           (rarely useful)
      - ""          → one flat series (all points pooled)
    """
    out: list[tuple[str, float, str]] = []
    for run in doc.get("runs", []):
        # Schema (a): metrics nested under run["metrics"]
        val = None
        if "metrics" in run and isinstance(run["metrics"], dict):
            val = _get_dotted(run["metrics"], metric_path)
        # Schema (b): metric directly on run
        if val is None:
            val = _get_dotted(run, metric_path)
        if not isinstance(val, (int, float)) or math.isnan(float(val)):
            continue

        if series_key:
            series = str(run.get(series_key, "default"))
        else:
            series = "default"

        ts = str(run.get("timestamp") or doc.get("timestamp") or "")
        out.append((series, float(val), ts))
    return out


def load_history(history_dir: Path, metric_path: str, series_key: str,
                 window: int) -> dict[str, list[float]]:
    """
    Return {series: [values]} using the most recent `window` points.
    Points are ordered by embedded timestamp (falling back to file mtime).
    Works whether history_dir contains one aggregate file with many runs, or
    one file per run.
    """
    if not history_dir.exists():
        return {}

    # Load every file, tagging each point with its sort key.
    points: list[tuple[str, str, float]] = []  # (sort_key, series, value)
    for f in sorted(history_dir.glob("*.json")):
        try:
            doc = json.loads(f.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        file_mtime = f.stat().st_mtime
        for series, val, ts in _extract_points(doc, metric_path, series_key):
            sort_key = ts if ts else f"@mtime:{file_mtime:.6f}"
            points.append((sort_key, series, val))

    # Group by series, then keep the most-recent `window` per series.
    by_series: dict[str, list[tuple[str, float]]] = {}
    for sort_key, series, val in points:
        by_series.setdefault(series, []).append((sort_key, val))

    result: dict[str, list[float]] = {}
    for series, pts in by_series.items():
        pts.sort(key=lambda p: p[0])
        result[series] = [v for _, v in pts[-window:]]
    return result


def load_current(current_path: Path, metric_path: str,
                 series_key: str) -> dict[str, float]:
    """Load the current summary. Returns {series: current_value}. If the file
    contains multiple runs per series, the last one wins (that's the intended
    behavior — the "current" run is the newest)."""
    doc = json.loads(current_path.read_text())
    out: dict[str, float] = {}
    for series, val, _ts in _extract_points(doc, metric_path, series_key):
        out[series] = val   # last write wins → newest run
    return out


# --------------------------------------------------------------------------- #
# Regression evaluation                                                       #
# --------------------------------------------------------------------------- #

def evaluate(current: dict[str, float],
             history: dict[str, list[float]],
             *,
             metric_path: str,
             direction: str,
             rel_threshold: float | None,
             abs_threshold: float | None,
             outlier_mad_k: float,
             min_history: int) -> list[RegressionFinding]:
    findings: list[RegressionFinding] = []

    for series, cur_val in current.items():
        raw = history.get(series, [])
        kept, dropped = filter_outliers(raw, k=outlier_mad_k)

        if len(kept) < min_history:
            findings.append(RegressionFinding(
                series=series,
                metric_path=metric_path,
                direction=direction,
                current_value=cur_val,
                baseline_median=float("nan"),
                baseline_mad=float("nan"),
                baseline_n_used=len(kept),
                baseline_n_dropped_as_outlier=len(dropped),
                delta_abs=float("nan"),
                delta_rel=float("nan"),
                regressed=False,
                reason=(f"Insufficient history: {len(kept)} clean samples "
                        f"(need ≥ {min_history}) — skipped."),
                outliers_dropped=dropped,
            ))
            continue

        base_med = median(kept)
        base_mad = mad(kept, center=base_med)
        delta_abs = cur_val - base_med
        delta_rel = (delta_abs / base_med) if base_med != 0 else 0.0

        # Direction-aware sign of regression.
        if direction == "lower_is_better":
            regressed_abs = (abs_threshold is not None
                             and delta_abs > abs_threshold)
            regressed_rel = (rel_threshold is not None
                             and delta_rel > rel_threshold)
            sign = "+"
        elif direction == "higher_is_better":
            # A drop of X counts as +X regression.
            regressed_abs = (abs_threshold is not None
                             and -delta_abs > abs_threshold)
            regressed_rel = (rel_threshold is not None
                             and -delta_rel > rel_threshold)
            sign = "-"
        else:
            raise ValueError(f"Unknown direction: {direction!r}")

        reasons: list[str] = []
        regressed = regressed_abs or regressed_rel
        if regressed_abs:
            reasons.append(
                f"absolute Δ {delta_abs:+.3f} exceeds {sign}{abs_threshold}")
        if regressed_rel:
            reasons.append(
                f"relative Δ {delta_rel:+.1%} exceeds "
                f"{sign}{rel_threshold:.0%}")
        if not regressed:
            reasons.append(
                f"within thresholds (Δ {delta_rel:+.1%}, "
                f"MAD-based noise floor ≈ ±{(base_mad / base_med):.1%})"
                if base_med else "within thresholds")

        findings.append(RegressionFinding(
            series=series,
            metric_path=metric_path,
            direction=direction,
            current_value=cur_val,
            baseline_median=base_med,
            baseline_mad=base_mad,
            baseline_n_used=len(kept),
            baseline_n_dropped_as_outlier=len(dropped),
            delta_abs=delta_abs,
            delta_rel=delta_rel,
            regressed=regressed,
            reason="; ".join(reasons),
            outliers_dropped=dropped,
        ))
    return findings


# --------------------------------------------------------------------------- #
# Rendering                                                                   #
# --------------------------------------------------------------------------- #

def render_markdown(findings: list[RegressionFinding],
                    metric_path: str,
                    direction: str,
                    rel_threshold: float | None,
                    abs_threshold: float | None) -> str:
    thresh_bits = []
    if abs_threshold is not None:
        thresh_bits.append(f"absolute > {abs_threshold}")
    if rel_threshold is not None:
        thresh_bits.append(f"relative > {rel_threshold:.0%}")
    thresh_str = " OR ".join(thresh_bits) if thresh_bits else "none configured"

    any_regressed = any(f.regressed for f in findings)
    header_icon = "🔴" if any_regressed else "🟢"
    header = (f"{header_icon} **{metric_path} regression check** "
              f"({direction.replace('_', ' ')}) — thresholds: {thresh_str}")

    if not findings:
        return f"{header}\n\n_No comparable metric data found._"

    rows = [
        "| Series | Current | Baseline median | MAD | n | outliers | Δ abs | Δ % | Status |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | :---: |",
    ]
    for f in findings:
        if math.isnan(f.baseline_median):
            row = (f"| {f.series} | {f.current_value:.2f} | _n/a_ | _n/a_ "
                   f"| {f.baseline_n_used} | {f.baseline_n_dropped_as_outlier} "
                   f"| — | — | ⚪ warmup |")
        else:
            icon = "🔴 regressed" if f.regressed else "🟢 ok"
            row = (f"| {f.series} | {f.current_value:.2f} "
                   f"| {f.baseline_median:.2f} | {f.baseline_mad:.2f} "
                   f"| {f.baseline_n_used} | {f.baseline_n_dropped_as_outlier} "
                   f"| {f.delta_abs:+.2f} | {f.delta_rel:+.1%} | {icon} |")
        rows.append(row)
    return header + "\n\n" + "\n".join(rows)


# --------------------------------------------------------------------------- #
# CLI                                                                         #
# --------------------------------------------------------------------------- #

def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--current", type=Path, required=True,
                   help="Current run summary JSON.")
    p.add_argument("--history-dir", type=Path, required=True,
                   help="Directory of prior summary JSON files.")
    p.add_argument("--metric", default="latency_ms.p99",
                   help="Dotted metric path, e.g. 'latency_ms.p99' or "
                        "'throughput'. Default: latency_ms.p99")
    p.add_argument("--series-key", default="env",
                   help="Run field used to group into series. "
                        "'label' for benchmark_viz summaries, 'env' for the "
                        "flat history schema, '' to pool all runs.")
    p.add_argument("--direction",
                   choices=("lower_is_better", "higher_is_better"),
                   default="lower_is_better")
    p.add_argument("--rel-threshold", type=float, default=0.10,
                   help="Relative regression threshold (default 0.10 = 10%%).")
    p.add_argument("--abs-threshold", type=float, default=None,
                   help="Optional absolute threshold in the metric's unit.")
    p.add_argument("--history-window", type=int, default=8,
                   help="Number of most-recent points to use as baseline "
                        "(post-outlier-filter). Default 8.")
    p.add_argument("--outlier-mad-k", type=float, default=3.5,
                   help="Modified Z-score cutoff for outlier drops "
                        "(default 3.5).")
    p.add_argument("--min-history", type=int, default=3,
                   help="Minimum clean baseline samples required before a "
                        "regression can be flagged (default 3).")
    p.add_argument("--output", type=Path, default=None)
    p.add_argument("--markdown", type=Path, default=None)
    return p.parse_args(argv)


def run(args: argparse.Namespace) -> tuple[dict, str, int]:
    current = load_current(args.current, args.metric, args.series_key)
    history = load_history(args.history_dir, args.metric, args.series_key,
                           args.history_window)

    findings = evaluate(
        current, history,
        metric_path=args.metric,
        direction=args.direction,
        rel_threshold=args.rel_threshold,
        abs_threshold=args.abs_threshold,
        outlier_mad_k=args.outlier_mad_k,
        min_history=args.min_history,
    )

    report = {
        "metric_path": args.metric,
        "direction": args.direction,
        "rel_threshold": args.rel_threshold,
        "abs_threshold": args.abs_threshold,
        "history_window": args.history_window,
        "outlier_mad_k": args.outlier_mad_k,
        "min_history": args.min_history,
        "regressed": any(f.regressed for f in findings),
        "findings": [asdict(f) for f in findings],
    }
    md = render_markdown(findings, args.metric, args.direction,
                         args.rel_threshold, args.abs_threshold)
    exit_code = 1 if report["regressed"] else 0
    return report, md, exit_code


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.abs_threshold is None and args.rel_threshold is None:
        print("ERROR: provide --abs-threshold and/or --rel-threshold",
              file=sys.stderr)
        return 2
    try:
        report, md, code = run(args)
    except FileNotFoundError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2

    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2))
    if args.markdown:
        args.markdown.parent.mkdir(parents=True, exist_ok=True)
        args.markdown.write_text(md)
    print(md)
    return code


if __name__ == "__main__":
    sys.exit(main())
