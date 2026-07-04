#!/usr/bin/env python3
"""
Aggregate benchmark JSON files into a trend dataset and render charts.

Reads every *.json under --input-dir (recursively), extracts numeric
metrics, groups them by timestamp, then emits:

  - trend.json  : long-format records (timestamp, metric, value)
  - trend.csv   : same data, spreadsheet-friendly
  - trend.png   : static line chart per metric (embeddable in README)
  - index.html  : interactive Plotly chart for GitHub Pages

Supported input shapes (auto-detected):

  A) {"timestamp": "...", "metrics": {"metric_a": 12.3, "metric_b": 4.5}}
  B) {"timestamp": "...", "results": [{"name": "metric_a", "value": 12.3}, ...]}
  C) Flat: {"timestamp": "...", "metric_a": 12.3, "metric_b": 4.5}

If a file has no "timestamp" field, the filename is parsed for an ISO-like
stamp (e.g. run-2026-07-02T06-00-00Z.json), then falls back to file mtime.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import matplotlib

matplotlib.use("Agg")  # headless
import matplotlib.dates as mdates
import matplotlib.pyplot as plt


TS_IN_NAME = re.compile(
    r"(\d{4}-\d{2}-\d{2}[T_ ]\d{2}[-:]\d{2}[-:]\d{2}Z?)"
)


# ---------- parsing --------------------------------------------------------

def parse_timestamp(raw: str) -> datetime | None:
    if not raw:
        return None
    s = raw.strip().replace("_", "T")
    # Accept both "2026-07-02T06-00-00Z" (filename-safe) and ISO 8601.
    if re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}Z?$", s):
        s = s[:10] + "T" + s[11:13] + ":" + s[14:16] + ":" + s[17:19]
        if not s.endswith("Z"):
            s += "Z"
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None


def timestamp_for(path: Path, payload: dict) -> datetime:
    ts = parse_timestamp(str(payload.get("timestamp", "")))
    if ts:
        return ts
    m = TS_IN_NAME.search(path.name)
    if m:
        ts = parse_timestamp(m.group(1))
        if ts:
            return ts
    return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)


def extract_metrics(payload: Any) -> dict[str, float]:
    """Return {metric_name: value} for whichever shape the JSON uses."""
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
        # Flat shape — top-level numeric keys, excluding reserved names.
        reserved = {"timestamp", "metrics", "results", "commit", "run_id", "sha"}
        for k, v in payload.items():
            if k in reserved:
                continue
            add(str(k), v)
    return metrics


def load_files(input_dir: Path) -> list[dict]:
    records: list[dict] = []
    for path in sorted(input_dir.rglob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            print(f"skip {path}: {exc}", file=sys.stderr)
            continue
        ts = timestamp_for(path, payload if isinstance(payload, dict) else {})
        metrics = extract_metrics(payload)
        if not metrics:
            print(f"skip {path}: no numeric metrics found", file=sys.stderr)
            continue
        for name, value in metrics.items():
            records.append(
                {
                    "timestamp": ts.isoformat().replace("+00:00", "Z"),
                    "metric": name,
                    "value": value,
                    "source": path.name,
                }
            )
    records.sort(key=lambda r: (r["timestamp"], r["metric"]))
    return records


# ---------- outputs --------------------------------------------------------

def write_json(records: list[dict], out: Path) -> None:
    out.write_text(json.dumps(records, indent=2), encoding="utf-8")


def write_csv(records: list[dict], out: Path) -> None:
    with out.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["timestamp", "metric", "value", "source"])
        writer.writeheader()
        writer.writerows(records)


def by_metric(records: Iterable[dict]) -> dict[str, list[tuple[datetime, float]]]:
    grouped: dict[str, list[tuple[datetime, float]]] = {}
    for r in records:
        ts = datetime.fromisoformat(r["timestamp"].replace("Z", "+00:00"))
        grouped.setdefault(r["metric"], []).append((ts, r["value"]))
    for series in grouped.values():
        series.sort(key=lambda p: p[0])
    return grouped


def render_png(records: list[dict], out: Path) -> None:
    grouped = by_metric(records)
    if not grouped:
        return
    fig, (ax_raw, ax_idx) = plt.subplots(
        2, 1, figsize=(11, 8), dpi=140, sharex=True
    )
    for metric, series in sorted(grouped.items()):
        xs = [p[0] for p in series]
        ys = [p[1] for p in series]
        ax_raw.plot(xs, ys, marker="o", markersize=3, linewidth=1.5, label=metric)
        # Indexed: first sample = 100, so regressions across metrics of
        # different magnitudes are visually comparable.
        base = next((v for v in ys if v not in (0, None)), None)
        if base:
            idx = [(v / base) * 100 for v in ys]
            ax_idx.plot(xs, idx, marker="o", markersize=3, linewidth=1.5, label=metric)
    ax_raw.set_title("Benchmark trend (raw values)")
    ax_raw.set_ylabel("Value")
    ax_raw.grid(True, linestyle="--", alpha=0.4)
    ax_raw.legend(loc="best", fontsize=8, framealpha=0.9)

    ax_idx.axhline(100, color="#888", linewidth=0.8, linestyle=":")
    ax_idx.set_title("Indexed to first run = 100")
    ax_idx.set_xlabel("Run timestamp (UTC)")
    ax_idx.set_ylabel("Index")
    ax_idx.grid(True, linestyle="--", alpha=0.4)
    ax_idx.xaxis.set_major_formatter(mdates.DateFormatter("%Y-%m-%d"))
    fig.autofmt_xdate()
    fig.tight_layout()
    fig.savefig(out)
    plt.close(fig)


HTML_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <title>Benchmark trend</title>
  <meta name="viewport" content="width=device-width,initial-scale=1" />
  <script src="https://cdn.plot.ly/plotly-2.35.2.min.js"></script>
  <style>
    :root { color-scheme: light dark; }
    body { font: 14px/1.5 system-ui, -apple-system, Segoe UI, sans-serif;
           margin: 0; padding: 24px; max-width: 1200px; margin-inline: auto; }
    h1 { margin: 0 0 4px; font-size: 20px; }
    p.muted { color: #666; margin: 0 0 20px; }
    #chart { width: 100%; height: 70vh; min-height: 480px; }
    footer { margin-top: 16px; color: #888; font-size: 12px; }
  </style>
</head>
<body>
  <h1>Benchmark trend</h1>
  <p class="muted">Generated __GENERATED_AT__ &middot; __RUN_COUNT__ runs &middot; __METRIC_COUNT__ metrics</p>
  <div id="chart"></div>
  <footer>Data: <a href="trend.json">trend.json</a> &middot; <a href="trend.csv">trend.csv</a> &middot; static <a href="trend.png">PNG</a></footer>
  <script>
    const data = __TRACES__;
    const layout = {
      margin: { t: 20, r: 20, b: 60, l: 60 },
      xaxis: { title: "Run timestamp (UTC)", type: "date" },
      yaxis: { title: "Value", rangemode: "tozero" },
      hovermode: "x unified",
      legend: { orientation: "h", y: -0.2 },
      template: (window.matchMedia("(prefers-color-scheme: dark)").matches
                 ? "plotly_dark" : "plotly_white")
    };
    Plotly.newPlot("chart", data, layout, {responsive: true, displaylogo: false});
  </script>
</body>
</html>
"""


def render_html(records: list[dict], out: Path) -> None:
    grouped = by_metric(records)
    traces = []
    for metric, series in sorted(grouped.items()):
        traces.append(
            {
                "type": "scatter",
                "mode": "lines+markers",
                "name": metric,
                "x": [p[0].isoformat().replace("+00:00", "Z") for p in series],
                "y": [p[1] for p in series],
                "hovertemplate": f"<b>{metric}</b><br>%{{x}}<br>%{{y}}<extra></extra>",
            }
        )
    unique_ts = {r["timestamp"] for r in records}
    html = (
        HTML_TEMPLATE
        .replace("__TRACES__", json.dumps(traces))
        .replace("__GENERATED_AT__", datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"))
        .replace("__RUN_COUNT__", str(len(unique_ts)))
        .replace("__METRIC_COUNT__", str(len(grouped)))
    )
    out.write_text(html, encoding="utf-8")


# ---------- entrypoint -----------------------------------------------------

def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--input-dir", type=Path, default=Path("benchmarks"),
                   help="Directory containing benchmark JSON files (recursive).")
    p.add_argument("--output-dir", type=Path, default=Path("site"),
                   help="Where to write trend.json/csv/png and index.html.")
    args = p.parse_args()

    if not args.input_dir.exists():
        print(f"error: --input-dir {args.input_dir} does not exist", file=sys.stderr)
        return 2

    args.output_dir.mkdir(parents=True, exist_ok=True)
    records = load_files(args.input_dir)
    if not records:
        print("error: no benchmark records found", file=sys.stderr)
        return 1

    write_json(records, args.output_dir / "trend.json")
    write_csv(records, args.output_dir / "trend.csv")
    render_png(records, args.output_dir / "trend.png")
    render_html(records, args.output_dir / "index.html")

    print(f"wrote {len(records)} records across "
          f"{len({r['metric'] for r in records})} metrics to {args.output_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
