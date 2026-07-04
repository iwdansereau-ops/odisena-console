#!/usr/bin/env python3
"""
build_report.py — aggregate lbload JSON results into a comparison report.

Usage:
    python3 build_report.py <results_dir> <report.md> <report.csv>

Expects filenames like:  {impl}_{cores}c_run{N}.json
Groups by (impl, cores). For each cell, computes:
    * median, mean, stdev of achieved rate
    * median, mean, stdev of p50/p99/p99.9/p99.99/max latencies
Then joins rwmutex vs atomic per core count and reports:
    * Speedup = rwmutex.p99_median / atomic.p99_median
    * Latency reduction % = 1 - atomic/rwmutex
    * Pass/Fail vs the 1.7x lower bound established on the 2-core sandbox.

The report is written both as GitHub-Actions-friendly Markdown (report.md)
and machine-readable CSV (report.csv).
"""

from __future__ import annotations

import csv
import json
import math
import re
import sys
from pathlib import Path
from statistics import mean, median, pstdev
from typing import Any

LOWER_BOUND_SPEEDUP = 1.7
FILENAME_RE = re.compile(r"^(?P<impl>rwmutex|atomic)_(?P<cores>\d+)c_run(?P<run>\d+)\.json$")

LATENCY_KEYS = ["p50", "p75", "p90", "p99", "p99_9", "p99_99", "max", "mean"]


def load_results(results_dir: Path) -> dict[tuple[str, int], list[dict[str, Any]]]:
    """Group JSON files by (impl, cores)."""
    cells: dict[tuple[str, int], list[dict[str, Any]]] = {}
    for path in sorted(results_dir.glob("*.json")):
        m = FILENAME_RE.match(path.name)
        if not m:
            print(f"WARN: skipping unrecognized file {path.name}", file=sys.stderr)
            continue
        with path.open() as f:
            data = json.load(f)
        impl = m.group("impl")
        cores = int(m.group("cores"))
        data["_filename"] = path.name
        data["_run"] = int(m.group("run"))
        cells.setdefault((impl, cores), []).append(data)
    return cells


def safe_stdev(vals: list[float]) -> float:
    return pstdev(vals) if len(vals) > 1 else 0.0


def summarize_cell(runs: list[dict[str, Any]]) -> dict[str, Any]:
    """Compute per-cell aggregate stats across N runs."""
    rates = [r["achieved_rate_per_sec"] for r in runs]
    summary: dict[str, Any] = {
        "n_runs": len(runs),
        "achieved_rate_median": median(rates),
        "achieved_rate_mean": mean(rates),
        "achieved_rate_stdev": safe_stdev(rates),
        "target_rate": runs[0]["target_rate_per_sec"],
        "workers": runs[0]["workers"],
        "gomaxprocs": runs[0]["gomaxprocs"],
        "num_cpu": runs[0]["num_cpu"],
        "total_ops_sum": sum(r["total_ops"] for r in runs),
        "dropped_records_sum": sum(r.get("dropped_records", 0) for r in runs),
    }
    for k in LATENCY_KEYS:
        vals = [r["latency_ns"][k] for r in runs]
        summary[f"lat_{k}_median_ns"] = median(vals)
        summary[f"lat_{k}_mean_ns"] = mean(vals)
        summary[f"lat_{k}_stdev_ns"] = safe_stdev(vals)
    return summary


def fmt_ns(ns: float) -> str:
    """Human-readable latency."""
    if ns >= 1e9:
        return f"{ns / 1e9:.2f}s"
    if ns >= 1e6:
        return f"{ns / 1e6:.2f}ms"
    if ns >= 1e3:
        return f"{ns / 1e3:.1f}µs"
    return f"{ns:.0f}ns"


def fmt_rate(r: float) -> str:
    if r >= 1e6:
        return f"{r / 1e6:.2f}M/s"
    if r >= 1e3:
        return f"{r / 1e3:.1f}k/s"
    return f"{r:.0f}/s"


def build_markdown(
    cell_summaries: dict[tuple[str, int], dict[str, Any]],
    all_cores: list[int],
) -> str:
    lines: list[str] = []
    lines.append("# OTel Exporter Concurrency Load Test — Comparison Report")
    lines.append("")
    lines.append(
        f"Compares `sync.RWMutex` vs `atomic.Pointer[T]` implementations of the "
        f"consistent-hash router at the 100k trace-ID/sec workload. Each cell is the "
        f"median across N runs."
    )
    lines.append("")
    lines.append(f"**Lower-bound speedup to confirm:** ≥ **{LOWER_BOUND_SPEEDUP}×** "
                 "(established on the 2-core sandbox benchmark).")
    lines.append("")

    # --------------------------------------------------------------
    # Table 1: headline p99 comparison
    # --------------------------------------------------------------
    lines.append("## Headline: p99 ConsumeTraces latency, RWMutex vs Atomic")
    lines.append("")
    lines.append(
        "| Cores | Runs | RWMutex p99 | Atomic p99 | Speedup (rw/atomic) | "
        "Latency reduction | ≥1.7× lower bound? |"
    )
    lines.append("|---:|---:|---:|---:|---:|---:|:---:|")
    for cores in all_cores:
        rw = cell_summaries.get(("rwmutex", cores))
        at = cell_summaries.get(("atomic", cores))
        if not rw or not at:
            missing = []
            if not rw:
                missing.append("rwmutex")
            if not at:
                missing.append("atomic")
            lines.append(
                f"| {cores} | — | — | — | — | — | ⚠️ missing: {', '.join(missing)} |"
            )
            continue
        rw_p99 = rw["lat_p99_median_ns"]
        at_p99 = at["lat_p99_median_ns"]
        speedup = rw_p99 / at_p99 if at_p99 > 0 else float("nan")
        reduction = (1 - at_p99 / rw_p99) * 100 if rw_p99 > 0 else float("nan")
        passfail = "✅ yes" if speedup >= LOWER_BOUND_SPEEDUP else "❌ no"
        lines.append(
            f"| {cores} | {rw['n_runs']}/{at['n_runs']} "
            f"| {fmt_ns(rw_p99)} | {fmt_ns(at_p99)} "
            f"| **{speedup:.2f}×** | {reduction:+.1f}% | {passfail} |"
        )
    lines.append("")

    # --------------------------------------------------------------
    # Table 2: full latency distribution per impl
    # --------------------------------------------------------------
    lines.append("## Full latency distribution (median of N runs)")
    lines.append("")
    lines.append(
        "| Cores | Impl | Runs | Achieved rate | p50 | p90 | p99 | p99.9 | p99.99 | max |"
    )
    lines.append("|---:|:---|---:|---:|---:|---:|---:|---:|---:|---:|")
    for cores in all_cores:
        for impl in ("rwmutex", "atomic"):
            s = cell_summaries.get((impl, cores))
            if not s:
                continue
            lines.append(
                f"| {cores} | `{impl}` | {s['n_runs']} "
                f"| {fmt_rate(s['achieved_rate_median'])} "
                f"| {fmt_ns(s['lat_p50_median_ns'])} "
                f"| {fmt_ns(s['lat_p90_median_ns'])} "
                f"| {fmt_ns(s['lat_p99_median_ns'])} "
                f"| {fmt_ns(s['lat_p99_9_median_ns'])} "
                f"| {fmt_ns(s['lat_p99_99_median_ns'])} "
                f"| {fmt_ns(s['lat_max_median_ns'])} |"
            )
    lines.append("")

    # --------------------------------------------------------------
    # Table 3: run-to-run variance (p99 stdev)
    # --------------------------------------------------------------
    lines.append("## Run-to-run variance (p99 latency, stdev across runs)")
    lines.append("")
    lines.append("| Cores | Impl | p99 median | p99 stdev | Coefficient of variation |")
    lines.append("|---:|:---|---:|---:|---:|")
    for cores in all_cores:
        for impl in ("rwmutex", "atomic"):
            s = cell_summaries.get((impl, cores))
            if not s:
                continue
            med = s["lat_p99_median_ns"]
            sd = s["lat_p99_stdev_ns"]
            cv = (sd / med * 100) if med > 0 else float("nan")
            lines.append(
                f"| {cores} | `{impl}` "
                f"| {fmt_ns(med)} | {fmt_ns(sd)} | {cv:.1f}% |"
            )
    lines.append("")

    # --------------------------------------------------------------
    # Interpretation section
    # --------------------------------------------------------------
    lines.append("## Interpretation")
    lines.append("")

    # Find largest core count where both impls have data
    max_cores_with_both = max(
        (c for c in all_cores
         if ("rwmutex", c) in cell_summaries and ("atomic", c) in cell_summaries),
        default=None,
    )
    if max_cores_with_both is not None:
        rw = cell_summaries[("rwmutex", max_cores_with_both)]
        at = cell_summaries[("atomic", max_cores_with_both)]
        top_speedup = rw["lat_p99_median_ns"] / at["lat_p99_median_ns"]
        verdict = (
            f"At the highest core count in this run ({max_cores_with_both} cores), the "
            f"atomic.Pointer refactor delivers a **{top_speedup:.2f}× p99 speedup** vs "
            f"sync.RWMutex."
        )
        if top_speedup >= LOWER_BOUND_SPEEDUP:
            verdict += (
                f" This **confirms the ≥{LOWER_BOUND_SPEEDUP}× lower bound** measured on "
                f"the 2-core sandbox holds at production-like core counts."
            )
        else:
            verdict += (
                f" This is **below** the ≥{LOWER_BOUND_SPEEDUP}× lower bound. Check that "
                f"(a) rate is high enough to saturate the router, (b) workers = 2× cores "
                f"is honored, (c) the workload is not blocked upstream (dropped_records "
                f"should be 0)."
            )
        lines.append(verdict)
        lines.append("")

    # Dropped records check
    total_drops = sum(s["dropped_records_sum"] for s in cell_summaries.values())
    if total_drops > 0:
        lines.append(
            f"> ⚠️ **{total_drops:,} latency records were dropped** across all runs "
            f"(scheduled-time queue overflow — coordinated-omission safeguard tripped). "
            f"Interpret p99.9/p99.99 with caution; the generator could not keep up with "
            f"the target rate."
        )
        lines.append("")

    lines.append("### How to read the speedup column")
    lines.append("")
    lines.append(
        "Speedup = `p99(rwmutex) / p99(atomic)`. A speedup of **1.0×** means no "
        "improvement; **1.7×** means atomic is 41% faster at the tail; **2.0×** means "
        "atomic is 50% faster; **5.0×** means the RWMutex reader-counter cache-line "
        "ping-pong (see [golang/go#17973](https://github.com/golang/go/issues/17973)) "
        "is actively degrading throughput."
    )
    lines.append("")
    lines.append("---")
    lines.append(
        "*Generated by `scripts/build_report.py`. "
        "Source: [otel-collector-contrib loadbalancingexporter]"
        "(https://github.com/open-telemetry/opentelemetry-collector-contrib/tree/main/exporter/loadbalancingexporter).*"
    )
    return "\n".join(lines) + "\n"


def write_csv(
    cell_summaries: dict[tuple[str, int], dict[str, Any]],
    csv_path: Path,
) -> None:
    fieldnames = [
        "impl", "cores", "n_runs", "workers", "gomaxprocs", "num_cpu",
        "target_rate", "achieved_rate_median", "achieved_rate_mean",
        "achieved_rate_stdev", "total_ops_sum", "dropped_records_sum",
    ]
    for k in LATENCY_KEYS:
        fieldnames += [f"lat_{k}_median_ns", f"lat_{k}_mean_ns", f"lat_{k}_stdev_ns"]

    with csv_path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for (impl, cores), s in sorted(cell_summaries.items(), key=lambda kv: (kv[0][1], kv[0][0])):
            row = {"impl": impl, "cores": cores}
            row.update({k: s[k] for k in fieldnames if k in s})
            w.writerow(row)


def main(argv: list[str]) -> int:
    if len(argv) != 4:
        print(__doc__, file=sys.stderr)
        return 2
    results_dir = Path(argv[1])
    md_out = Path(argv[2])
    csv_out = Path(argv[3])

    if not results_dir.is_dir():
        print(f"ERROR: {results_dir} is not a directory", file=sys.stderr)
        return 1

    cells = load_results(results_dir)
    if not cells:
        print(f"ERROR: no matching JSON files found in {results_dir}", file=sys.stderr)
        print("Expected filenames: {impl}_{cores}c_run{N}.json", file=sys.stderr)
        return 1

    cell_summaries = {k: summarize_cell(v) for k, v in cells.items()}
    all_cores = sorted({cores for _, cores in cells.keys()})

    md = build_markdown(cell_summaries, all_cores)
    md_out.write_text(md)
    write_csv(cell_summaries, csv_out)

    print(f"Wrote {md_out} ({len(md)} bytes)")
    print(f"Wrote {csv_out}")
    print(f"Cells found: {sorted(cells.keys())}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
