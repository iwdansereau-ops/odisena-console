# OTel Pipeline Performance Budget Scorecard & Threshold Configuration Guide

**Scope:** Nightly benchmark runs of an OpenTelemetry Collector pipeline (receivers → processors → exporters), executed in GitHub Actions, compared against a rolling 7-day baseline of successful production runs. This guide defines what to measure, how much drift is acceptable, how to keep cloud CI noise from causing false alarms, and how to automatically block merges when the budget is blown.

---

## 1. Scorecard: Metrics to Monitor

The scorecard groups metrics into four budget classes. Each class has a hard **budget** (absolute SLO), a **warning threshold** (relative drift vs. 7-day baseline), and a **failure threshold** (relative drift vs. 7-day baseline). Absolute budgets prevent baseline rot — a slowly degrading baseline should not be allowed to hide a real regression.

### 1.1 Latency (per-signal, per-exporter)

| Metric | Definition | Absolute Budget | Warn (Δ vs 7d) | Fail (Δ vs 7d) |
|---|---|---|---|---|
| `otelcol_exporter_send_latency_p50_ms` | Median end-to-end latency: receive → export ack | ≤ 25 ms | +5% | +10% |
| `otelcol_exporter_send_latency_p95_ms` | 95th percentile | ≤ 75 ms | +5% | +10% |
| `otelcol_exporter_send_latency_p99_ms` | 99th percentile — the tail metric that drives user pain | ≤ 200 ms | +7% | +15% |
| `otelcol_processor_batch_send_latency_p99_ms` | Batch processor flush latency | ≤ 50 ms | +7% | +15% |
| `queue_wait_time_p99_ms` | Time a span sits in the sending queue | ≤ 100 ms | +10% | +20% |

Rationale: p99 gets slightly looser drift tolerance than p50/p95 because tail metrics are inherently noisier — tightening them would just generate false failures ([CodSpeed on CI noise](https://codspeed.io/blog/benchmarks-in-ci-without-noise)).

### 1.2 Throughput

| Metric | Definition | Absolute Budget | Warn (Δ vs 7d) | Fail (Δ vs 7d) |
|---|---|---|---|---|
| `spans_per_second` | Sustained span ingestion rate (per logical core, then averaged) — matches the [OpenTelemetry benchmark spec](https://opentelemetry.io/docs/specs/otel/performance-benchmark/) | ≥ 10,000 sps/core | −5% | −10% |
| `metric_points_per_second` | Sustained metric datapoint rate | ≥ 50,000 dps/core | −5% | −10% |
| `logs_per_second` | Sustained log record rate | ≥ 20,000 lps/core | −5% | −10% |
| `export_success_ratio` | `sent / (sent + failed)` for OTLP exporter | ≥ 0.999 | −0.1 pp | −0.5 pp |

Note the sign inversion: for throughput, a **decrease** vs baseline is bad, so thresholds are negative deltas.

### 1.3 Resource Cost (per-span / per-datapoint)

Normalizing CPU and memory by work done keeps the scorecard valid across workload sizes. This mirrors the OTel spec's "measure at a fixed throughput of 10,000 spans/sec" approach.

| Metric | Definition | Absolute Budget | Warn (Δ vs 7d) | Fail (Δ vs 7d) |
|---|---|---|---|---|
| `cpu_ns_per_span` | (CPU seconds × 1e9) / spans processed, averaged over the run | ≤ 15,000 ns/span | +5% | +10% |
| `cpu_peak_percent` | Peak CPU% (matches OTel spec's peak reporting requirement) | ≤ 85% | +5% | +10% |
| `heap_bytes_per_span` | Peak heap / spans in flight | ≤ 4 KiB/span | +7% | +15% |
| `rss_peak_mib` | Peak RSS of the Collector process | ≤ 512 MiB | +5% | +10% |
| `gc_pause_p99_ms` | Go runtime GC p99 pause (if Collector is Go-based) | ≤ 5 ms | +10% | +25% |

### 1.4 Reliability & Backpressure

| Metric | Definition | Absolute Budget | Warn (Δ vs 7d) | Fail (Δ vs 7d) |
|---|---|---|---|---|
| `dropped_spans_ratio` | `refused + dropped / accepted` | ≤ 0.0001 | +0.05 pp | +0.1 pp |
| `queue_saturation_max` | Peak `queue_size / queue_capacity` | ≤ 0.7 | +10% | +20% |
| `retry_ratio` | Retried batches / total batches | ≤ 0.01 | +0.5 pp | +1 pp |

---

## 2. Baseline Calculation: Rolling 7-Day Window

### 2.1 What counts as a baseline sample

- Only **successful** nightly runs from the `main` branch (exit 0, all checks green).
- Rolling window = **last 7 successful runs** (not last 7 calendar days — a broken week shouldn't collapse the sample size).
- Each metric summarized per run as the **median of N=10 iterations** (aligns with the OTel spec's ≥10-measurement requirement).
- Runs older than 21 calendar days are dropped even if inside the last 7 successful — prevents stale baselines during long freezes.

### 2.2 Baseline statistics

For each metric, compute from the 7 baseline runs:

- `μ_base` — mean of the 7 per-run medians
- `σ_base` — sample standard deviation
- `CV_base = σ_base / μ_base` — coefficient of variation
- `MAD_base` — median absolute deviation (robust fallback when CV is unstable)

### 2.3 Drift calculation

For a candidate run with per-metric median `x_cand`:

```
relative_drift = (x_cand - μ_base) / μ_base           # signed
z_score        = (x_cand - μ_base) / σ_base           # noise-aware
robust_z       = 0.6745 × (x_cand - median_base) / MAD_base
```

The scorecard fails if **both** of the following are true:

1. `|relative_drift|` exceeds the metric's **Fail** threshold from §1, **and**
2. `|robust_z| ≥ 3.0` (statistically distinguishable from baseline noise)

Both conditions must hold. Threshold-only gating fires on any noisy runner; z-only gating misses slow drift when noise is high. Requiring both is the standard approach used by [CodSpeed and other CI benchmark tools](https://codspeed.io/blog/benchmarks-in-ci-without-noise).

---

## 3. Handling Cloud CI Noise

GitHub-hosted runners exhibit CV of 5–15% on wall-clock CPU benchmarks — enough to trigger a naïve 5% threshold roughly every other run. The following tactics bring effective noise floor below 2%.

### 3.1 Runner selection

- **Use `runs-on: ubuntu-latest-large` or a self-hosted runner with pinned CPU generation.** Standard `ubuntu-latest` runs on shared VMs with variable CPU steal.
- Pin to a **single runner class** for baseline + candidate runs. Never compare an Intel run to an AMD run.
- Record `/proc/cpuinfo model name` and reject the run if the CPU family drifts from the baseline majority.

### 3.2 Statistical hygiene per run

- **Warmup:** discard the first 30 seconds of every iteration (JIT / cache warmup, per OTel spec).
- **N = 10 iterations** minimum per metric per run (OTel spec floor); use the median, not the mean.
- **Iteration length ≥ 60 seconds** (OTel spec floor is 15 s; longer is quieter).
- **Reject the run entirely** if intra-run CV > 20% — the runner is too noisy to draw conclusions. Retry once, then post a `benchmark-unstable` label instead of failing the merge.
- **Trim outliers:** drop the top and bottom iteration before computing the run's median (10 → 8 samples).

### 3.3 Environmental controls

- Pre-pull all container images and warm the filesystem cache before the timed section.
- Disable Actions caching for the benchmark step's working directory to avoid disk-cache variance.
- Set process affinity via `taskset -c 0-3` and cap the Collector to a fixed CPU quota (`--cpus=2.0` in the docker run).
- Run the load generator on a **separate runner** and stream OTLP over loopback via `services:` in Actions — keeps the load generator's CPU out of the Collector's measurement.
- Fix `GOMAXPROCS`, `GOGC=100`, and any language-specific tuning to constant values.

### 3.4 Confidence via re-run on failure

If a candidate run fails **only one** metric and `|robust_z|` is in the [3.0, 4.0) borderline zone, automatically re-run once. If both runs fail on the same metric, that's a real regression. If only one, it's noise — pass with a warning annotation. This is essentially a two-of-three vote and eliminates the majority of flaky failures without hiding real regressions.

---

## 4. GitHub Actions YAML Template

Save as `.github/workflows/otel-perf-nightly.yml`. Merge-blocking is enforced by making this check **required** in branch protection.

```yaml
name: OTel Pipeline Performance Budget

on:
  schedule:
    - cron: '0 6 * * *'          # 06:00 UTC nightly
  pull_request:
    paths:
      - 'collector/**'
      - 'config/**'
      - '.github/workflows/otel-perf-nightly.yml'
  workflow_dispatch:

concurrency:
  group: otel-perf-${{ github.ref }}
  cancel-in-progress: false      # never cancel a running benchmark

permissions:
  contents: read
  pull-requests: write            # for regression comment
  checks: write                   # for the required status check
  actions: read

jobs:
  perf-budget:
    name: Performance Budget Gate
    runs-on: ubuntu-latest-large  # pinned larger runner, less noisy
    timeout-minutes: 45

    env:
      ITERATIONS: 10
      ITERATION_SECONDS: 60
      WARMUP_SECONDS: 30
      GOMAXPROCS: 4
      GOGC: 100
      BASELINE_WINDOW: 7          # last 7 successful main runs
      MAX_INTRA_RUN_CV: 0.20      # reject run if noisier than this
      RETRY_ON_BORDERLINE: true

    steps:
      - name: Checkout
        uses: actions/checkout@v4
        with:
          fetch-depth: 0

      - name: Verify runner CPU family
        run: |
          model=$(grep -m1 "model name" /proc/cpuinfo | cut -d: -f2 | xargs)
          echo "runner_cpu=$model" >> "$GITHUB_ENV"
          echo "Detected CPU: $model"

      - name: Pull baseline from artifact store
        id: baseline
        uses: actions/download-artifact@v4
        with:
          name: otel-perf-baseline-main
          path: ./baseline
        continue-on-error: true   # first run has no baseline

      - name: Build Collector image
        run: docker build -t otelcol-bench:${{ github.sha }} ./collector

      - name: Warm caches (untimed)
        run: |
          docker run --rm otelcol-bench:${{ github.sha }} --version
          docker pull otel/opentelemetry-collector-contrib:latest

      - name: Run benchmark suite
        id: bench
        run: |
          taskset -c 0-3 ./scripts/run-bench.sh \
            --iterations "$ITERATIONS" \
            --duration   "$ITERATION_SECONDS" \
            --warmup     "$WARMUP_SECONDS" \
            --output     ./results/current.json

      - name: Reject run if too noisy
        run: |
          python3 ./scripts/check_intra_cv.py \
            --results ./results/current.json \
            --max-cv  "$MAX_INTRA_RUN_CV"

      - name: Evaluate against 7-day baseline
        id: evaluate
        run: |
          python3 ./scripts/evaluate_budget.py \
            --current   ./results/current.json \
            --baseline  ./baseline/ \
            --config    ./perf-budget.yaml \
            --report    ./results/scorecard.md \
            --junit     ./results/scorecard.xml \
            --exit-on   fail
        continue-on-error: true   # capture status, decide below

      - name: Borderline re-run
        if: >
          steps.evaluate.outcome == 'failure' &&
          env.RETRY_ON_BORDERLINE == 'true' &&
          fromJSON(steps.evaluate.outputs.borderline_only || 'false')
        run: |
          taskset -c 0-3 ./scripts/run-bench.sh \
            --iterations "$ITERATIONS" \
            --duration   "$ITERATION_SECONDS" \
            --warmup     "$WARMUP_SECONDS" \
            --output     ./results/rerun.json
          python3 ./scripts/evaluate_budget.py \
            --current  ./results/rerun.json \
            --baseline ./baseline/ \
            --config   ./perf-budget.yaml \
            --report   ./results/scorecard.md \
            --exit-on  fail

      - name: Publish scorecard artifact
        if: always()
        uses: actions/upload-artifact@v4
        with:
          name: otel-perf-scorecard-${{ github.run_id }}
          path: ./results/

      - name: Comment scorecard on PR
        if: github.event_name == 'pull_request' && always()
        uses: marocchino/sticky-pull-request-comment@v2
        with:
          header: otel-perf-budget
          path: ./results/scorecard.md

      - name: Publish JUnit results
        if: always()
        uses: mikepenz/action-junit-report@v4
        with:
          report_paths: ./results/scorecard.xml
          check_name: OTel Perf Budget

      - name: Update rolling baseline (main only, on success)
        if: >
          github.ref == 'refs/heads/main' &&
          github.event_name == 'schedule' &&
          steps.evaluate.outcome == 'success'
        uses: actions/upload-artifact@v4
        with:
          name: otel-perf-baseline-main
          path: ./results/current.json
          retention-days: 21

      - name: Fail the check if budget exceeded
        if: steps.evaluate.outcome == 'failure'
        run: |
          echo "::error::Performance budget exceeded — see scorecard artifact."
          exit 1
```

### 4.1 Companion `perf-budget.yaml`

```yaml
# perf-budget.yaml — thresholds consumed by evaluate_budget.py
baseline:
  window: 7                      # last N successful main runs
  max_age_days: 21
  min_samples: 3                 # abstain if fewer baselines exist

gate:
  require_relative_and_zscore: true
  zscore_fail_threshold: 3.0
  borderline_zscore_range: [3.0, 4.0]

metrics:
  # LATENCY
  - name: exporter_send_latency_p50_ms
    direction: increase_is_bad
    absolute_budget_max: 25
    warn_relative: 0.05
    fail_relative: 0.10
  - name: exporter_send_latency_p95_ms
    direction: increase_is_bad
    absolute_budget_max: 75
    warn_relative: 0.05
    fail_relative: 0.10
  - name: exporter_send_latency_p99_ms
    direction: increase_is_bad
    absolute_budget_max: 200
    warn_relative: 0.07
    fail_relative: 0.15
  - name: processor_batch_send_latency_p99_ms
    direction: increase_is_bad
    absolute_budget_max: 50
    warn_relative: 0.07
    fail_relative: 0.15
  - name: queue_wait_time_p99_ms
    direction: increase_is_bad
    absolute_budget_max: 100
    warn_relative: 0.10
    fail_relative: 0.20

  # THROUGHPUT
  - name: spans_per_second
    direction: decrease_is_bad
    absolute_budget_min: 10000
    warn_relative: -0.05
    fail_relative: -0.10
  - name: metric_points_per_second
    direction: decrease_is_bad
    absolute_budget_min: 50000
    warn_relative: -0.05
    fail_relative: -0.10
  - name: logs_per_second
    direction: decrease_is_bad
    absolute_budget_min: 20000
    warn_relative: -0.05
    fail_relative: -0.10
  - name: export_success_ratio
    direction: decrease_is_bad
    absolute_budget_min: 0.999
    warn_absolute_delta: -0.001
    fail_absolute_delta: -0.005

  # RESOURCE COST
  - name: cpu_ns_per_span
    direction: increase_is_bad
    absolute_budget_max: 15000
    warn_relative: 0.05
    fail_relative: 0.10
  - name: cpu_peak_percent
    direction: increase_is_bad
    absolute_budget_max: 85
    warn_relative: 0.05
    fail_relative: 0.10
  - name: heap_bytes_per_span
    direction: increase_is_bad
    absolute_budget_max: 4096
    warn_relative: 0.07
    fail_relative: 0.15
  - name: rss_peak_mib
    direction: increase_is_bad
    absolute_budget_max: 512
    warn_relative: 0.05
    fail_relative: 0.10
  - name: gc_pause_p99_ms
    direction: increase_is_bad
    absolute_budget_max: 5
    warn_relative: 0.10
    fail_relative: 0.25

  # RELIABILITY
  - name: dropped_spans_ratio
    direction: increase_is_bad
    absolute_budget_max: 0.0001
    warn_absolute_delta: 0.0005
    fail_absolute_delta: 0.001
  - name: queue_saturation_max
    direction: increase_is_bad
    absolute_budget_max: 0.7
    warn_relative: 0.10
    fail_relative: 0.20
  - name: retry_ratio
    direction: increase_is_bad
    absolute_budget_max: 0.01
    warn_absolute_delta: 0.005
    fail_absolute_delta: 0.01

reporting:
  markdown: true
  junit: true
  slack_webhook_env: SLACK_PERF_WEBHOOK
  pr_comment: true
```

### 4.2 Branch protection

In **Settings → Branches → main → Require status checks**, add `Performance Budget Gate` as a required check. This is what actually blocks merges — the workflow's `exit 1` alone doesn't block anything without branch protection.

---

## 5. Automated Regression Reporting

The `evaluate_budget.py` script emits three artifacts per run.

### 5.1 Markdown scorecard (PR comment)

Rendered as a sticky comment on the PR and as a job summary. Layout:

```markdown
### OTel Perf Budget — commit abc1234

**Verdict:** ❌ FAIL (2 metrics regressed, 1 warning)

| Metric | Baseline μ ± σ | Current | Δ | z | Budget | Status |
|---|---|---|---|---|---|---|
| exporter_send_latency_p99_ms | 142.3 ± 6.1 | 168.0 | +18.1% | +4.2 | +15% | ❌ FAIL |
| cpu_ns_per_span | 12,400 ± 380 | 13,900 | +12.1% | +3.9 | +10% | ❌ FAIL |
| rss_peak_mib | 410 ± 12 | 435 | +6.1% | +2.1 | +10% | ⚠️ WARN |
| spans_per_second | 11,800 ± 210 | 11,720 | −0.7% | −0.4 | −10% | ✅ PASS |
| ...

**Runner:** ubuntu-latest-large · Intel Xeon Platinum 8370C
**Iterations:** 10 × 60s (warmup 30s) · Intra-run CV: 3.2%
**Baseline:** last 7 successful main runs (2026-06-25 → 2026-07-01)

<details><summary>Sparklines vs 30-day trend</summary>
[embedded PNG or ASCII sparkline per metric]
</details>
```

### 5.2 JUnit XML

Emitted for the GitHub Checks UI so each metric appears as an individual test case. Failed metrics show up as failed tests, warnings as skipped-with-message. This makes flaky metrics easy to spot in the Actions test analytics view.

### 5.3 Slack / notification hook

On `fail` on `main` (i.e., a regression landed via emergency merge or by a scheduled run against a broken baseline), post to the webhook in `SLACK_PERF_WEBHOOK`:

```
🚨 OTel perf regression on main @ abc1234
  • exporter_send_latency_p99_ms: 142.3 → 168.0 ms (+18%, z=4.2)
  • cpu_ns_per_span: 12.4k → 13.9k (+12%, z=3.9)
  Scorecard: https://github.com/org/repo/actions/runs/…
  Compare:   https://github.com/org/repo/compare/abc0000...abc1234
```

### 5.4 Long-term trend dashboard

Every scorecard JSON is uploaded to the artifact store with 90-day retention. A separate weekly workflow (`perf-trend.yml`) pulls the last 30 days of artifacts and renders a Grafana-ready CSV per metric so long-slope drift (< fail threshold per night, but +30% over a month) is visible.

---

## 6. Rollout Checklist

- [ ] Land `perf-budget.yaml` and `evaluate_budget.py` on `main`.
- [ ] Run the nightly workflow 7 times unblocked to seed the baseline artifact.
- [ ] Verify intra-run CV is < 5% on the pinned runner class; if not, revisit §3.
- [ ] Turn on `Performance Budget Gate` as a required check in branch protection.
- [ ] Wire `SLACK_PERF_WEBHOOK` in repo secrets.
- [ ] Schedule the weekly trend workflow.
- [ ] Document the escape hatch: `perf-budget-override` label on a PR skips the gate but requires a linked follow-up issue (enforced by a separate lightweight action).

---

## 7. Sources

- [OpenTelemetry Performance Benchmark spec](https://opentelemetry.io/docs/specs/otel/performance-benchmark/) — iteration count, warmup, throughput and CPU/memory methodology
- [CodSpeed: Benchmarks in CI without noise](https://codspeed.io/blog/benchmarks-in-ci-without-noise) — coefficient of variation, cloud CI noise floors
- [k6 as a release gate in GitHub Actions](https://www.sabaoon.dev/blog/performance-testing-k6-github-actions) — SLO-based build failure patterns
- [github-action-benchmark](https://github.com/benchmark-action/github-action-benchmark) — reference implementation for baseline storage and PR commenting
