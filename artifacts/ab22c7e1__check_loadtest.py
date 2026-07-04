#!/usr/bin/env python3
"""Parse loadtest_summary.txt and enforce CI thresholds.

Reads a summary file produced by TestContinuousChurn200msGCBudget and fails
(exit code 1) if any of the configured budgets are exceeded. Prints a
GitHub-Actions-friendly report to stdout and emits `$GITHUB_STEP_SUMMARY`
markdown when that env var is set so the numbers show up on the run page
without needing to open the artifact.

Thresholds (defaults match the memo):
  --max-ingest-ms      default 200
  --max-gc-pause-ms    default 10

The summary file format is line-oriented; we look for two specific lines:

  GC:
    ...
    pause:            p50=… p90=… p99=… max=<duration>
  ingest:
    max latency:      <duration>

Durations are Go time.Duration strings like "1.234ms", "482.61µs",
"52.024622ms", or a compound like "1m2.3s". We parse them into seconds.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path


# ---- duration parsing --------------------------------------------------------

# Handles Go's stringified time.Duration values. Examples:
#   "482.61µs" "1.234ms" "52.024622ms" "1.2s" "2m3.4s" "1h2m3.4s"
# Unicode 'µ' and ASCII 'u' both appear depending on locale.
_UNIT_S = {
    "ns": 1e-9,
    "us": 1e-6,
    "µs": 1e-6,
    "μs": 1e-6,  # micro sign (U+00B5) *and* Greek mu (U+03BC) — Go emits U+00B5
    "ms": 1e-3,
    "s": 1.0,
    "m": 60.0,
    "h": 3600.0,
}
# Order matters: match longer unit strings before shorter ones (so "ms" beats "s").
_UNIT_ORDER = ["ns", "us", "µs", "μs", "ms", "h", "m", "s"]
_UNIT_ALT = "|".join(_UNIT_ORDER)
_TOKEN_RE = re.compile(r"([0-9]*\.?[0-9]+)\s*(" + _UNIT_ALT + r")")


def parse_duration_seconds(text: str) -> float:
    """Parse a Go-formatted duration into seconds.

    Raises ValueError if no valid tokens are found.
    """
    text = text.strip()
    tokens = _TOKEN_RE.findall(text)
    if not tokens:
        raise ValueError(f"could not parse duration: {text!r}")
    total = 0.0
    for value, unit in tokens:
        total += float(value) * _UNIT_S[unit]
    return total


# ---- summary parsing --------------------------------------------------------

def extract_max(line: str) -> str:
    """Return the token following 'max=' on a line (used for GC pause line).

    Example: 'pause: p50=… p99=… max=1.23ms' -> '1.23ms'
    """
    m = re.search(r"max=([^\s]+)", line)
    if not m:
        raise ValueError(f"no max= token in line: {line!r}")
    return m.group(1)


def parse_summary(path: Path) -> dict:
    """Extract the metrics we assert on.

    Returns a dict with keys: max_ingest_s, max_gc_pause_s, over_budget_calls,
    peak_state, peak_heap_mib. Missing keys are omitted.
    """
    metrics: dict = {}
    section: str | None = None

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.rstrip()
        if not line.strip():
            continue

        # Section headers end with a colon and are not indented.
        if not line.startswith(" ") and line.endswith(":"):
            section = line[:-1].strip().lower()
            continue

        stripped = line.strip()

        # GC pause line is under 'GC:' section, looks like:
        #   pause: p50=… p90=… p99=… max=<duration>
        if section == "gc" and stripped.startswith("pause:"):
            token = extract_max(stripped)
            metrics["max_gc_pause_s"] = parse_duration_seconds(token)
            continue

        # Ingest max latency:
        #   max latency: <duration>
        if section == "ingest" and stripped.startswith("max latency:"):
            _, _, val = stripped.partition(":")
            metrics["max_ingest_s"] = parse_duration_seconds(val.strip())
            continue

        # Over-budget call count:
        #   over-budget: <n> calls
        if section == "ingest" and stripped.startswith("over-budget:"):
            m = re.search(r"over-budget:\s*(\d+)", stripped)
            if m:
                metrics["over_budget_calls"] = int(m.group(1))
            continue

        # Peak state size / heap — useful context, not enforced.
        if section == "state map":
            m = re.match(r"peak size:\s*(\d+)", stripped)
            if m:
                metrics["peak_state"] = int(m.group(1))
                continue
            m = re.match(r"peak heap:\s*([0-9.]+)\s*MiB", stripped)
            if m:
                metrics["peak_heap_mib"] = float(m.group(1))
                continue

    return metrics


# ---- reporting --------------------------------------------------------------

def write_step_summary(lines: list[str]) -> None:
    """Append a markdown block to GITHUB_STEP_SUMMARY if configured."""
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    with open(path, "a", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def fmt_ms(seconds: float) -> str:
    return f"{seconds * 1000:.2f} ms"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument(
        "--summary",
        default="loadtest_summary.txt",
        help="path to loadtest_summary.txt (default: %(default)s)",
    )
    ap.add_argument(
        "--max-ingest-ms",
        type=float,
        default=200.0,
        help="fail if max ingest latency exceeds this (default: %(default)s ms)",
    )
    ap.add_argument(
        "--max-gc-pause-ms",
        type=float,
        default=10.0,
        help="fail if max GC pause exceeds this (default: %(default)s ms)",
    )
    args = ap.parse_args()

    path = Path(args.summary)
    if not path.exists():
        print(f"::error::summary file not found: {path}", file=sys.stderr)
        return 2

    try:
        metrics = parse_summary(path)
    except Exception as exc:  # noqa: BLE001
        print(f"::error::failed to parse {path}: {exc}", file=sys.stderr)
        return 2

    required = ("max_ingest_s", "max_gc_pause_s")
    missing = [k for k in required if k not in metrics]
    if missing:
        print(
            f"::error::summary is missing required metrics: {missing}. "
            f"Parsed: {metrics}",
            file=sys.stderr,
        )
        return 2

    max_ingest_ms = metrics["max_ingest_s"] * 1000
    max_gc_pause_ms = metrics["max_gc_pause_s"] * 1000
    ingest_ok = max_ingest_ms <= args.max_ingest_ms
    gc_ok = max_gc_pause_ms <= args.max_gc_pause_ms

    # Console output.
    print("=" * 60)
    print("load test threshold check")
    print("=" * 60)
    print(f"  summary file:         {path}")
    if "peak_state" in metrics:
        print(f"  peak state size:      {metrics['peak_state']:,}")
    if "peak_heap_mib" in metrics:
        print(f"  peak heap:            {metrics['peak_heap_mib']:.1f} MiB")
    print(
        f"  max GC pause:         {fmt_ms(metrics['max_gc_pause_s'])} "
        f"(budget {args.max_gc_pause_ms:.0f} ms) "
        f"{'✅' if gc_ok else '❌'}"
    )
    print(
        f"  max ingest latency:   {fmt_ms(metrics['max_ingest_s'])} "
        f"(budget {args.max_ingest_ms:.0f} ms) "
        f"{'✅' if ingest_ok else '❌'}"
    )
    if "over_budget_calls" in metrics:
        print(f"  over-budget calls:    {metrics['over_budget_calls']}")

    # GitHub step-summary markdown.
    summary_lines = [
        "## Load test thresholds",
        "",
        "| Metric | Value | Budget | Result |",
        "|---|---:|---:|:---:|",
        (
            f"| Max GC pause | {fmt_ms(metrics['max_gc_pause_s'])} | "
            f"{args.max_gc_pause_ms:.0f} ms | {'✅' if gc_ok else '❌'} |"
        ),
        (
            f"| Max ingest latency | {fmt_ms(metrics['max_ingest_s'])} | "
            f"{args.max_ingest_ms:.0f} ms | {'✅' if ingest_ok else '❌'} |"
        ),
    ]
    if "peak_state" in metrics:
        summary_lines.append(
            f"| Peak state size | {metrics['peak_state']:,} | — | — |"
        )
    if "peak_heap_mib" in metrics:
        summary_lines.append(
            f"| Peak heap | {metrics['peak_heap_mib']:.1f} MiB | — | — |"
        )
    if "over_budget_calls" in metrics:
        summary_lines.append(
            f"| Over-budget ConsumeMetrics calls | {metrics['over_budget_calls']} | — | — |"
        )
    write_step_summary(summary_lines)

    # GitHub Actions annotations for failures.
    if not gc_ok:
        print(
            f"::error title=GC pause budget exceeded::"
            f"max GC pause {fmt_ms(metrics['max_gc_pause_s'])} > "
            f"{args.max_gc_pause_ms:g} ms",
            file=sys.stderr,
        )
    if not ingest_ok:
        print(
            f"::error title=Ingest latency budget exceeded::"
            f"max ingest latency {fmt_ms(metrics['max_ingest_s'])} > "
            f"{args.max_ingest_ms:g} ms",
            file=sys.stderr,
        )

    return 0 if (ingest_ok and gc_ok) else 1


if __name__ == "__main__":
    sys.exit(main())
