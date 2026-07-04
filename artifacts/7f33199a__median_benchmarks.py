#!/usr/bin/env python3
"""
Reduce N benchmark JSON files into a single median-per-metric JSON.

Usage:
    python median_benchmarks.py \\
        --inputs benchmarks/run-*.json \\
        --output benchmarks/median.json

For each metric found across the input files:
  - median       : statistics.median of the samples (robust to outliers)
  - min / max    : range of observed samples
  - stdev        : population stdev (0 for n<2)
  - cv           : coefficient of variation (stdev / |median|) as a percent
  - samples      : count of valid numeric samples

The output document preserves the shape expected by compare_benchmarks.py
(the `metrics` map) and additionally includes `stats` for diagnostics and
a `directions` passthrough if any input file specified one.
"""

from __future__ import annotations

import argparse
import glob
import json
import statistics
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


# ---- shape-agnostic metric extraction (same rules as the other scripts) ---

def extract_metrics(payload: Any) -> dict[str, float]:
    metrics: dict[str, float] = {}

    def add(name: str, value: Any) -> None:
        if isinstance(value, bool):
            return
        if isinstance(value, (int, float)):
            metrics[name] = float(value)

    if isinstance(payload, dict):
        if isinstance(payload.get("metrics"), dict):
            for k, v in payload["metrics"].items():
                add(str(k), v)
        if isinstance(payload.get("results"), list):
            for item in payload["results"]:
                if not isinstance(item, dict):
                    continue
                name = item.get("name") or item.get("metric") or item.get("id")
                value = item.get("value")
                if value is None:
                    value = item.get("mean") or item.get("median")
                if name is not None and value is not None:
                    add(str(name), value)
        reserved = {"timestamp", "metrics", "results", "commit", "run_id",
                    "sha", "directions"}
        for k, v in payload.items():
            if k in reserved:
                continue
            add(str(k), v)
    return metrics


def extract_directions(payload: Any) -> dict[str, str]:
    if isinstance(payload, dict) and isinstance(payload.get("directions"), dict):
        return {str(k): str(v).lower() for k, v in payload["directions"].items()}
    return {}


# ---- aggregation ----------------------------------------------------------

def collect_samples(paths: list[Path]) -> tuple[dict[str, list[float]], dict[str, str]]:
    samples: dict[str, list[float]] = {}
    directions: dict[str, str] = {}
    for path in paths:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            print(f"skip {path}: {exc}", file=sys.stderr)
            continue
        directions.update(extract_directions(payload))
        for name, value in extract_metrics(payload).items():
            samples.setdefault(name, []).append(value)
    return samples, directions


def summarize(values: list[float]) -> dict[str, float]:
    med = statistics.median(values)
    lo, hi = min(values), max(values)
    sd = statistics.pstdev(values) if len(values) > 1 else 0.0
    cv = (sd / abs(med) * 100.0) if med else 0.0
    return {
        "median": med,
        "min": lo,
        "max": hi,
        "stdev": sd,
        "cv": cv,
        "samples": len(values),
    }


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--inputs", nargs="+", required=True,
                   help="One or more input files or glob patterns.")
    p.add_argument("--output", type=Path, required=True,
                   help="Where to write the median JSON.")
    p.add_argument("--min-samples", type=int, default=3,
                   help="Fail if fewer than N valid input files were parsed.")
    args = p.parse_args()

    # Expand any globs the shell didn't (e.g. quoted patterns).
    paths: list[Path] = []
    for pattern in args.inputs:
        matches = [Path(m) for m in glob.glob(pattern)]
        if matches:
            paths.extend(matches)
        elif Path(pattern).exists():
            paths.append(Path(pattern))
    paths = sorted(set(paths))

    if len(paths) < args.min_samples:
        print(f"error: found {len(paths)} input file(s), need at least "
              f"{args.min_samples}", file=sys.stderr)
        return 2

    samples, directions = collect_samples(paths)
    if not samples:
        print("error: no numeric metrics extracted from inputs", file=sys.stderr)
        return 2

    metrics: dict[str, float] = {}
    stats: dict[str, dict[str, float]] = {}
    for name, values in sorted(samples.items()):
        s = summarize(values)
        metrics[name] = s["median"]
        stats[name] = s

    output = {
        "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "metrics": metrics,
        "stats": stats,
        "source_runs": [str(p) for p in paths],
    }
    if directions:
        output["directions"] = directions

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(output, indent=2), encoding="utf-8")

    # Short log for CI: median + CV per metric.
    print(f"aggregated {len(paths)} runs into {args.output}")
    for name, s in stats.items():
        print(f"  {name}: median={s['median']:.4g}  "
              f"cv={s['cv']:.2f}%  range=[{s['min']:.4g}, {s['max']:.4g}]  "
              f"n={s['samples']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
