# OTel Pipeline — Performance Budget Scorecard & Threshold Guide

A production-grade recipe for gating nightly OpenTelemetry Collector merges on performance. Blocks a merge to `main` when the build regresses against the rolling 7-day baseline, is resilient to cloud CI noise, and produces a human-readable scorecard on every run.

## Repository layout

```
otel-perf-budget/
├── config/
│   └── performance-budget.yaml          # single source of truth: metrics, thresholds, noise policy
├── scripts/
│   └── compare_to_baseline.py           # comparator: adaptive thresholds + Welch's t-test + canary short-circuit
├── .github/workflows/
│   └── otel-nightly-perf.yml            # 5×-iteration bench → score → promote-baseline
├── reports/
│   ├── scorecard.md                     # generated per run (PR comment + step summary)
│   ├── scorecard.json                   # machine-readable, promoted to baseline S3
│   └── templates/                       # example fixtures used by the smoke test
└── README.md                            # this file
```

---

## 1 — What the scorecard measures

The budget is grouped into four categories. Each metric has a **warn %** (soft signal), **fail %** (blocks merge), and an **absolute floor** (below which drift is ignored to prevent noise near zero). All thresholds live in [`config/performance-budget.yaml`](config/performance-budget.yaml) — no hard-coding in workflow YAML.

### Latency budget (client-observed roundtrip)

| Metric | Warn | Fail | Rationale |
|---|---:|---:|---|
| E2E P50 latency | +5% | +10% | Median tracks the common case |
| E2E P95 latency | +5% | +10% | Where SLOs live |
| **E2E P99 latency** | **+7%** | **+15%** | Wider band — tails are naturally noisier ([OneUptime scaling guide](https://oneuptime.com/blog/post/2026-02-06-scale-opentelemetry-collector-high-throughput/view)) |

### Throughput budget

| Metric | Warn | Fail |
|---|---:|---:|
| Sustained throughput (spans/sec) | −5% | −10% |
| Peak throughput before refusal | −5% | −10% |

Sustained is measured after a 60-second warmup over a 10-minute steady-state window; peak is the max 1-minute rate before `otelcol_receiver_refused_spans_total` starts incrementing (the standard [ldb benchmark methodology](https://leobecker.net/posts/benchmarking-opentelemetry/)).

### Efficiency budget (per-span cost)

These are the highest-signal metrics. A change that leaves throughput unchanged but makes each span 10% more expensive is a real regression — and easy to miss without normalization.

| Metric | Warn | Fail | Source |
|---|---:|---:|---|
| CPU cores per 1000 spans/sec | +5% | +10% | [OneUptime right-sizing recipe](https://oneuptime.com/blog/post/2026-02-06-right-size-cpu-memory-opentelemetry-collector/view) |
| Memory MiB per 1000 spans/sec | +5% | +10% | Same |
| Heap allocation bytes per span | +5% | +10% | GC pressure canary — often the first thing regex/OTTL regressions move ([OneUptime CPU profiling](https://oneuptime.com/blog/post/2026-02-06-profile-optimize-opentelemetry-collector-cpu-usage/view)) |

### Reliability budget (zero-tolerance)

| Metric | Fail condition |
|---|---|
| Refused spans / sec | Any positive value above `absolute_floor=1` |
| Exporter send-failed / sec | Any positive value above `absolute_floor=1` |
| Exporter queue saturation | > +25% vs baseline **and** absolute >5% |
| Batch send size (P50) | −30% (indicates broken batching — the [Lightstep tuning guide](https://docs.lightstep.com/docs/otel-collector-prometheus-receiver-performance-tuning) flags this pattern) |

---

## 2 — How thresholds are calculated

### Baseline: trimmed mean of successful production runs

```yaml
baseline:
  window_days: 7
  min_samples: 5
  aggregation: trimmed_mean
  trim_pct: 10
```

We use a **trimmed mean** rather than a plain mean because cloud CI regularly produces outlier runs from noisy neighbors ([Heisler's cloud benchmark study](https://bheisler.github.io/post/benchmarking-in-the-cloud/) measured up to +3000% variance on single points). Trimming the top and bottom 10% removes those without discarding signal from real drift.

Only runs marked `success` in production are eligible — never runs from failed nightlies, otherwise a regression that lands but doesn't fail hard would poison future comparisons.

### Drift calculation

```
drift = (current - baseline) / baseline × 100
```

For `lower_is_worse` metrics (throughput, batch size), the sign is inverted so a positive drift always means "bad".

### Adaptive thresholds — the key noise-handling primitive

The [Codspeed 2025-07 analysis](https://codspeed.io/blog/benchmarks-in-ci-without-noise) shows GitHub-hosted runners produce a **2.66% coefficient of variation** on average, which means a naïve 2% performance gate produces a **~45% false-positive rate**. To keep false positives below 1%, the effective threshold must be at least ~2× the observed CV.

`compare_to_baseline.py` implements this:

```python
effective_threshold = max(base_threshold_pct, min_multiplier × observed_cv_pct)
```

With `min_threshold_multiplier_of_cv: 2.0`, if the baseline CV for a metric is 4%, the effective fail threshold rises from 10% to 8% *floor*, so 10% still holds. But if CV spikes to 6% (noisy runner day), the effective threshold expands to 12% — preventing a false fail. This is the adaptive-threshold pattern advocated by [Nicholas Nethercote for rustc perf](https://github.com/rustls/rustls/issues/1485).

### Statistical significance gate

For every metric we also run a **Welch's t-test** between the current run's per-sample distribution and the baseline's pooled samples ([FOSDEM 2026 measurement guide](https://kakkoyun.me/posts/fosdem-2026-measuring-software-performance/)). A metric only fails when **both**:

1. Drift exceeds the effective fail threshold, and
2. `p < alpha` (default `0.01`)

This blocks the "small mean shift with huge variance" failure mode that plain threshold gates hit constantly.

---

## 3 — Handling cloud-CI benchmark noise

Six mitigations, layered from cheapest to most impactful:

| # | Mitigation | Impact | Where |
|---|---|---|---|
| 1 | 60s warmup + 5-min steady-state window | JIT + cache warmup ([FOSDEM 2026 Tip 1](https://kakkoyun.me/posts/fosdem-2026-measuring-software-performance/)) | workflow `warmup` step |
| 2 | ≥30 samples per run, ≥5 repetitions | Cuts CV from 11.8% → 2.94% (same source) | `matrix.iteration: [1..5]` |
| 3 | IQR outlier filtering (k=1.5) per run | Removes GC pauses, transient throttles | `scripts/compare_to_baseline.py:iqr_filter` |
| 4 | Trimmed mean for baseline aggregation | Robust to noisy-neighbor days | `config/performance-budget.yaml:aggregation` |
| 5 | Adaptive thresholds (≥2× CV) + Welch's t-test | Keeps FP rate <1% | `compare_to_baseline.py:adapt_threshold` |
| 6 | Self-hosted bare-metal runner with SMT+cpufreq pinned | CV drops from ~3% → <1% ([Codspeed](https://codspeed.io/blog/benchmarks-in-ci-without-noise), [dev.to EC2 guide](https://dev.to/kienmarkdo/low-noise-ec2-benchmarking-a-practical-guide-19f0)) | runner label + `Pin runner to performance mode` step |

### Canary short-circuit

Before every run, a small `telemetrygen` canary (1000 spans/sec, 60 s) exercises a known-stable path. If canary drift exceeds `canary_max_drift_pct: 3.0`, the run exits code **2 (INFRA_NOISE)**: the gate is skipped, the infra team is paged, and no baseline update happens. This is the "known-workload sanity check" from the [dev.to EC2 low-noise guide](https://dev.to/kienmarkdo/low-noise-ec2-benchmarking-a-practical-guide-19f0) — it prevents runner problems from generating false code regressions.

### Change-point detection (roadmap)

For teams that want to move past threshold-based gating entirely, the [SREcon 2024 talk on change-point detection](https://www.usenix.org/conference/srecon24emea/presentation/fleming) demonstrates using ED-PELT on 30-day rolling series to identify *distribution shifts* rather than single-run breaches. `compare_to_baseline.py` exposes a `significance_test: change_point` option for a future ruptures-based implementation.

---

## 4 — The GitHub Actions workflow

Three jobs, gated in series:

### `bench` — 5 sequential iterations

- Runs on `[self-hosted, Linux, X64, otel-perf-runner]` — critical for signal.
- `max-parallel: 1` — never run two benchmark iterations concurrently on the same host. Even parallel jobs on separate GitHub-hosted runners produce inconsistent measurements ([Heisler](https://bheisler.github.io/post/benchmarking-in-the-cloud/)).
- Uses `taskset -c 0-3` to pin telemetrygen to cores away from the collector.
- Each iteration uploads its `run_N.json` as a separate artifact so partial failures don't lose data.

### `score` — merge iterations, fetch baseline, compare

- Merges the 5 iteration artifacts into a single `current.json` with per-metric sample arrays.
- Pulls the last 7 days of nightly baseline runs from `s3://otel-perf-baselines/nightly/` via OIDC.
- Runs `compare_to_baseline.py` — the exit code drives the outcome:
  - `0` PASS or WARN (merge allowed, WARN posts a comment)
  - `1` FAIL (job fails → branch protection blocks the merge)
  - `2` INFRA_NOISE (canary regressed — infra paged, gate skipped)
- Posts the markdown scorecard as a **sticky PR comment** and adds it to `$GITHUB_STEP_SUMMARY`.

### `promote-baseline` — only on green scheduled runs

- Runs only when `github.event_name == 'schedule'` **and** `overall == 'PASS'`.
- Writes today's run to the baseline bucket with a 90-day retention policy.
- The bucket is versioned; the fetch script picks the 7 most recent successful runs.

### Blocking the merge

Two pieces need to be in place in the repo settings:

1. **Branch protection rule** on `main`: require the `Score vs. 7-day baseline` check to succeed before merging.
2. **Required status checks**: add `otel-nightly-perf / Score vs. 7-day baseline` to the required list.

`workflow_dispatch` exposes a `dry_run` input so operators can preview a suspect commit without blocking.

---

## 5 — The generated scorecard

Every run produces both a JSON and a Markdown artifact. Here's an actual scorecard from the shipped fixtures showing a WARN outcome:

```
# OTel Nightly Performance Scorecard

**Overall:** ⚠️ **WARN** (0 fail, 2 warn, 10 pass)

| Metric                           | Current | Baseline | Drift  | Warn  | Fail   | p         | Status |
| End-to-end P99 latency           | 189.6ms | 180.8ms  | +4.90% | 7.00% | 15.00% | 0.00159   | ✅     |
| CPU cores per 1000 spans/sec     | 0.087   | 0.082    | +5.79% | 5.00% | 10.00% | 0.00011   | ⚠️     |
| Heap allocation bytes per span   | 3119    | 2968     | +5.09% | 5.00% | 10.00% | 2.02e-07  | ⚠️     |
...

## Warnings
- **CPU cores per 1000 spans/sec** — drift +5.79% exceeds warn threshold 5.00%
- **Heap allocation bytes per span (GC pressure)** — drift +5.09% exceeds warn threshold 5.00%
```

Note the p-values: both warned metrics are statistically significant (`p < 0.001`), which strengthens the signal. If the same drift had `p = 0.4`, the comparator would demote it to noise.

### Notifications

| Overall | Action |
|---|---|
| PASS | Silent. Run promoted to baseline on nightly schedule. |
| WARN | Sticky PR comment + step summary. Non-blocking. |
| FAIL | Sticky PR comment + step summary + Slack `#otel-perf-alerts` + workflow exit 1. |
| INFRA_NOISE | Slack `#otel-infra` with runner ID; gate skipped. |

---

## 6 — Adopting this in your repo

1. Copy `config/`, `scripts/`, and `.github/workflows/` into your repo.
2. Provision a self-hosted runner following the [dev.to low-noise EC2 guide](https://dev.to/kienmarkdo/low-noise-ec2-benchmarking-a-practical-guide-19f0); label it `otel-perf-runner`.
3. Create the S3 bucket `otel-perf-baselines` (or edit the URI) and set up the OIDC role in `secrets.PERF_BASELINE_ROLE`.
4. Add branch protection requiring the `Score vs. 7-day baseline` check on `main`.
5. Bootstrap the baseline by running `workflow_dispatch` with `dry_run: true` for 7 consecutive days before enabling the gate. Until `min_samples: 5` runs exist, every metric is advisory-only.
6. Tune per-metric `warn_pct` / `fail_pct` after 30 days of history — use the observed CV to justify each number.

### Common tuning traps

- **Setting warn=1% or fail=2% on GitHub-hosted runners.** Won't work; the runner noise floor is above the threshold. Use bare metal or widen thresholds.
- **Skipping the canary.** You will get pages for every AWS maintenance event. Keep the canary.
- **Comparing against yesterday only.** One noisy day poisons the next. Always aggregate over a window ≥ 5 runs.
- **Not tracking per-span efficiency.** Absolute CPU and memory scale with traffic — you need normalized metrics ([OneUptime right-sizing](https://oneuptime.com/blog/post/2026-02-06-right-size-cpu-memory-opentelemetry-collector/view)) to catch efficiency regressions during traffic dips.

---

## 7 — References

- [OpenTelemetry Collector benchmarks](https://opentelemetry.io/docs/collector/benchmarks/) — official per-commit load-test methodology
- [How to right-size the OTel Collector](https://oneuptime.com/blog/post/2026-02-06-right-size-cpu-memory-opentelemetry-collector/view) — per-span CPU/memory model this scorecard uses
- [Benchmarking highly distributed tracing in OTel (Becker 2022)](https://leobecker.net/posts/benchmarking-opentelemetry/) — peak-load methodology, saturation signals
- [Benchmarks in CI: escaping the cloud chaos (Codspeed 2025)](https://codspeed.io/blog/benchmarks-in-ci-without-noise) — CV / false-positive math behind the adaptive threshold
- [Low-noise EC2 benchmarking (MongoDB-derived guide 2026)](https://dev.to/kienmarkdo/low-noise-ec2-benchmarking-a-practical-guide-19f0) — self-hosted runner hardening
- [Measuring software performance (FOSDEM 2026)](https://kakkoyun.me/posts/fosdem-2026-measuring-software-performance/) — sample-count and repetition guidance
- [Taming noisy benchmark results with change-point detection (SREcon 2024)](https://www.usenix.org/conference/srecon24emea/presentation/fleming) — roadmap technique for post-threshold gating
- [rustls adaptive-threshold discussion](https://github.com/rustls/rustls/issues/1485) — Nicholas Nethercote's argument for CV-relative thresholds
- [Are CI benchmarks reliable? (Heisler 2018)](https://bheisler.github.io/post/benchmarking-in-the-cloud/) — original cloud-CI variance measurements
