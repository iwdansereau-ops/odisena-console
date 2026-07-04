#!/usr/bin/env python3
"""
Compare a PR's benchmark run against the latest baseline and emit a
Markdown summary plus an exit code that fails CI on regressions.

Usage:
    python compare_benchmarks.py \\
        --current benchmarks/latest.json \\
        --baseline baseline/latest.json \\
        --threshold 5 \\
        --output pr-comment.md

Direction of "better" is inferred from the metric name:

  - Names containing "latency", "time", "duration", "ms", "seconds",
    "memory", "bytes", "size", "cpu", "alloc", "error" → LOWER is better.
  - Names containing "throughput", "rps", "qps", "ops", "score", "hits",
    "success" → HIGHER is better.
  - Fallback: LOWER is better (safer for perf benchmarks).

Override per-metric with a `directions` map in either JSON file, e.g.
    {"metrics": {...}, "directions": {"score": "higher"}}

Exit codes:
    0 - no regressions beyond threshold
    1 - at least one regression beyond threshold (CI should fail)
    2 - usage / input error
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any


LOWER_BETTER_HINTS = (
    "latency", "time", "duration", "_ms", "ms_", "seconds", "sec_",
    "memory", "mem_", "bytes", "size", "cpu", "alloc", "error", "fail",
)
HIGHER_BETTER_HINTS = (
    "throughput", "rps", "qps", "ops", "score", "hits", "success", "hit_rate",
)


def infer_direction(metric: str, overrides: dict[str, str]) -> str:
    if metric in overrides:
        return overrides[metric].lower()
    lowered = metric.lower()
    for hint in HIGHER_BETTER_HINTS:
        if hint in lowered:
            return "higher"
    for hint in LOWER_BETTER_HINTS:
        if hint in lowered:
            return "lower"
    return "lower"  # safe default for perf suites


# --- Reuse the shape auto-detection from aggregate_benchmarks -------------

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


# --- Comparison -----------------------------------------------------------

def pct_delta(current: float, baseline: float) -> float | None:
    if baseline == 0:
        return None
    return (current - baseline) / abs(baseline) * 100.0


def classify(delta_pct: float, direction: str, threshold: float) -> str:
    """Return one of: 'regression', 'improvement', 'neutral'."""
    if abs(delta_pct) < threshold:
        return "neutral"
    if direction == "higher":
        return "improvement" if delta_pct > 0 else "regression"
    return "regression" if delta_pct > 0 else "improvement"


def format_delta(delta_pct: float) -> str:
    sign = "+" if delta_pct >= 0 else ""
    return f"{sign}{delta_pct:.2f}%"


def build_report(
    current: dict[str, float],
    baseline: dict[str, float],
    directions: dict[str, str],
    threshold: float,
) -> tuple[str, bool]:
    """Return (markdown_report, has_regression)."""
    all_metrics = sorted(set(current) | set(baseline))
    rows: list[dict] = []
    regressions: list[dict] = []
    improvements: list[dict] = []
    neutrals: list[dict] = []
    missing: list[str] = []
    added: list[str] = []

    for name in all_metrics:
        cur = current.get(name)
        base = baseline.get(name)
        direction = infer_direction(name, directions)

        if cur is None:
            missing.append(name)
            continue
        if base is None:
            added.append(name)
            rows.append({"metric": name, "baseline": None, "current": cur,
                         "delta": None, "class": "new", "direction": direction})
            continue

        delta = pct_delta(cur, base)
        if delta is None:
            rows.append({"metric": name, "baseline": base, "current": cur,
                         "delta": None, "class": "neutral", "direction": direction})
            continue

        cls = classify(delta, direction, threshold)
        row = {"metric": name, "baseline": base, "current": cur,
               "delta": delta, "class": cls, "direction": direction}
        rows.append(row)
        {"regression": regressions, "improvement": improvements,
         "neutral": neutrals}[cls].append(row)

    lines: list[str] = []
    lines.append("## Benchmark comparison")
    lines.append("")
    if regressions:
        head = f"**{len(regressions)} regression(s) beyond ±{threshold}% threshold.**"
    elif improvements:
        head = f"No regressions. {len(improvements)} improvement(s) detected."
    else:
        head = f"No metrics moved beyond ±{threshold}% threshold."
    lines.append(head)
    lines.append("")

    def emoji(cls: str) -> str:
        return {"regression": "🔴", "improvement": "🟢",
                "neutral": "⚪", "new": "🆕"}.get(cls, "⚪")

    def fmt_val(v: float | None) -> str:
        if v is None:
            return "—"
        if abs(v) >= 1000 or (v != 0 and abs(v) < 0.01):
            return f"{v:.4g}"
        return f"{v:.4f}".rstrip("0").rstrip(".")

    lines.append("| | Metric | Direction | Baseline | Current | Δ |")
    lines.append("|---|---|---|---:|---:|---:|")
    # Show regressions first, then improvements, then neutral, then new.
    order = {"regression": 0, "improvement": 1, "neutral": 2, "new": 3}
    for row in sorted(rows, key=lambda r: (order.get(r["class"], 9), r["metric"])):
        delta_str = format_delta(row["delta"]) if row["delta"] is not None else "—"
        arrow = "↑ better" if row["direction"] == "higher" else "↓ better"
        lines.append(
            f"| {emoji(row['class'])} | `{row['metric']}` | {arrow} | "
            f"{fmt_val(row['baseline'])} | {fmt_val(row['current'])} | {delta_str} |"
        )

    if missing:
        lines.append("")
        lines.append(f"Metrics in baseline but missing from current run: "
                     f"{', '.join(f'`{m}`' for m in missing)}")

    lines.append("")
    lines.append(f"<sub>Threshold: ±{threshold}% · "
                 f"lower-is-better inferred from metric name unless overridden.</sub>")
    lines.append("<!-- benchmark-comparison-marker -->")

    return "\n".join(lines), bool(regressions)


# --- Entrypoint -----------------------------------------------------------

def load_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"error: cannot read {path}: {exc}", file=sys.stderr)
        sys.exit(2)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--current", type=Path, required=True,
                   help="Benchmark JSON from the PR head.")
    p.add_argument("--baseline", type=Path, required=True,
                   help="Baseline benchmark JSON from benchmark-data branch.")
    p.add_argument("--threshold", type=float, default=5.0,
                   help="Percent change that counts as significant (default 5).")
    p.add_argument("--output", type=Path, required=True,
                   help="Where to write the Markdown comment body.")
    p.add_argument("--summary", type=Path, default=None,
                   help="Optional path to also write the report (e.g. $GITHUB_STEP_SUMMARY).")
    args = p.parse_args()

    cur_payload = load_json(args.current)
    base_payload = load_json(args.baseline)

    current = extract_metrics(cur_payload)
    baseline = extract_metrics(base_payload)

    if not current:
        print("error: no numeric metrics extracted from --current", file=sys.stderr)
        return 2
    if not baseline:
        print("error: no numeric metrics extracted from --baseline", file=sys.stderr)
        return 2

    directions = {**extract_directions(base_payload), **extract_directions(cur_payload)}

    report, has_regression = build_report(current, baseline, directions, args.threshold)

    args.output.write_text(report, encoding="utf-8")
    if args.summary:
        try:
            args.summary.write_text(report, encoding="utf-8")
        except OSError:
            pass

    # Also echo to stdout so it appears in the Actions log.
    print(report)
    return 1 if has_regression else 0


if __name__ == "__main__":
    raise SystemExit(main())
