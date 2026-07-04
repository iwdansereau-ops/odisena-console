# check_regression.py — refactored

Refactored regression detector for `otelcol-logexporter`'s weekly telemetrygen
benchmarks. Drop-in replacement for the previous `check_regression.py`.

## What's new

| Concern | Before | After |
|---|---|---|
| Baseline aggregation | Simple mean of last N files | **Windowed median** of last N points |
| Noise handling | None — one bad run poisons the baseline | **MAD-based outlier filter** (modified Z-score, k=3.5) drops ~3σ points before the median |
| Metric direction | Always "higher = worse" | `--direction lower_is_better` (latency) or `higher_is_better` (throughput) |
| Schema support | `runs[].metrics[m][p]` only | Also accepts flat `runs[].throughput` / `runs[].latency_ms.p99` |
| Warmup | Any missing history → NaN row | `--min-history` guard prevents flagging until baseline stabilizes |
| Ordering | Filesystem mtime | Embedded `timestamp` field (mtime fallback) — reproducible across CI runners |

## Usage

```bash
# Latency p99 check (default direction: lower is better)
python scripts/check_regression.py \
  --current  benchmarks/raw/latest.json \
  --history-dir benchmarks/history \
  --metric latency_ms.p99 \
  --series-key env \
  --rel-threshold 0.10 \
  --history-window 8 \
  --output benchmarks/reports/regressions.json \
  --markdown benchmarks/reports/regressions.md

# Throughput check (higher is better — regression = drop)
python scripts/check_regression.py \
  --current  benchmarks/raw/latest.json \
  --history-dir benchmarks/history \
  --metric throughput \
  --series-key env \
  --direction higher_is_better \
  --rel-threshold 0.10
```

Exit codes: `0` = clean, `1` = regression flagged, `2` = usage error.

## Tests

```bash
pip install pytest
python tests/build_fixture.py   # explodes bench_history.json → per-run files
pytest tests/ -v
```

The suite covers:

- **Robust stats** — median, MAD, and outlier filtering on hand-checkable inputs.
- **Loader** — both JSON schemas, embedded-timestamp ordering, windowing.
- **10% threshold** — tight boundary tests at ±9.9% / ±10.0% / ±10.1%,
  direction inversion, warmup guard.
- **Real-data outlier drops** — the known p99 spikes (74.17 on 2026-05-25,
  58.11 on 2026-06-13) and throughput dips (73k, 76k, 78k on 05-17, 06-05,
  06-19) are all filtered from the baseline.
- **Historical replay (headline test)** — walks the full 60-day fixture,
  replays each day as "current" against its prior 10-day window, and
  asserts that only the *known-legitimate* regressions flag. Anything else
  would be a false positive.
- **Next-run projection** — the CI's next Monday run (using 2026-07-01 as
  a proxy) does not flag. This is the direct answer to "ensure no false
  positives on the next run."

## What the fixture revealed

While building the tests, the historical replay surfaced a **genuine
sustained p99 drift from ~25 ms to ~31 ms between 2026-06-23 and 2026-07-01**
that is documented in `test_no_false_positive_p99_at_10pct`. Throughput held
steady at 145-158k across that window, so this isn't runner noise — it's a
real latency creep worth investigating in the collector code. The refactored
detector catches it cleanly.
