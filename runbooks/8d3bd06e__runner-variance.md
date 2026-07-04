# Runner Variance Analyzer

A companion to the OTel nightly performance gate that identifies which self-hosted CI runners are producing disproportionate noise, so you can quarantine them before their variance loosens your adaptive thresholds and lets real regressions merge.

## Why this exists

The nightly gate ([`otel-nightly-perf.yml`](../.github/workflows/otel-nightly-perf.yml)) uses adaptive thresholds that widen with observed CV — that's what makes the gate resilient to cloud noise. But it also means one persistently noisy host can *raise the noise floor for the entire fleet*, letting a real 6% regression slip through as within-noise. Detecting and quarantining that host is what this analyzer does.

## Prerequisite: extend the scorecard schema

The base scorecard doesn't currently record which runner produced it. Add these two fields in `compare_to_baseline.py` when it writes the JSON:

```python
payload = {
    "status": overall,
    "commit": os.environ.get("GITHUB_SHA", "local"),
    "run_url": os.environ.get("GITHUB_RUN_URL", ""),
    # NEW:
    "started_at": datetime.now(timezone.utc).isoformat(),
    "runner": {
        "name": os.environ.get("RUNNER_NAME", "unknown"),
        "labels": os.environ.get("RUNNER_LABELS", "").split(","),
    },
    # existing:
    "results": [asdict(r) for r in results],
    ...
}
```

`RUNNER_NAME` is set automatically by GitHub Actions on every runner (self-hosted and hosted). `RUNNER_LABELS` should be exported by the `bench` job:

```yaml
- name: Export runner labels
  run: echo "RUNNER_LABELS=self-hosted,otel-perf-runner,$(hostname)" >> "$GITHUB_ENV"
```

For richer analysis, also include the per-metric raw samples in the scorecard (the shipped `compare_to_baseline.py` already computes them internally). If only aggregated `current` values are available, the analyzer falls back to run-to-run variance instead of within-run variance — still useful, just lower resolution.

## Running

```bash
# Local
python scripts/analyze_runner_variance.py \
  --input-dir scorecards/ \
  --window-days 7 \
  --out-md   reports/runner-variance/runner_variance.md \
  --out-json reports/runner-variance/runner_variance.json

# CI (weekly, Monday 12:00 UTC)
gh workflow run otel-runner-variance.yml
```

The provided [`otel-runner-variance.yml`](../.github/workflows/otel-runner-variance.yml) does an `aws s3 sync` from the baseline bucket, runs the analyzer, posts to Slack, and auto-opens an issue tagged `infra,perf,runner-variance` when any runner is quarantined.

## How runners are scored

### Step 1 — Per-cell CV

For every `(runner, metric)` cell in the 7-day window, pool all samples that runner contributed and compute:

```
CV = stdev(samples) / |mean(samples)| × 100
```

Cells with fewer than `min_runs_per_runner` (3) runs or fewer than `min_samples_per_cell` (10) pooled samples are marked `INSUFFICIENT_DATA` — a new runner with only two nightlies shouldn't be branded noisy.

### Step 2 — Fleet reference distribution per metric

For each metric, compute the **median CV** and **MAD** (median absolute deviation) across all runners that produced enough data. Median + MAD are used instead of mean + stdev because they resist the very outlier we're trying to detect — a mean would be pulled up by the noisy runner and hide it.

### Step 3 — Iglewicz–Hoaglin modified z-score

Score each cell relative to its metric's fleet distribution:

```
robust_z = 0.6745 × (cell_cv − median_cv) / MAD
```

Any cell with `robust_z > 3.5` is flagged `HIGH_NOISE`. The 3.5 threshold is Iglewicz and Hoaglin's published recommendation — it corresponds to roughly p<0.001 for normally distributed data but degrades gracefully on skewed distributions (which benchmark CVs typically are).

### Step 4 — Per-runner rollup

- **QUARANTINE** if HIGH_NOISE on ≥30% of tracked metrics
- **WATCH** if HIGH_NOISE on 15–30% of tracked metrics
- **OK** otherwise
- **INSUFFICIENT_DATA** if <3 runs in window

The 30% quarantine threshold is deliberately loose: if the same host is bad across a third of your metrics, it's a host problem, not a metric-specific artifact.

## What the report tells you

```
| Runner            | Runs | Mean CV | High-noise metrics | Verdict         |
| perf-runner-06    |    7 |   5.89% | 5 / 5              | 🔴 QUARANTINE   |
| perf-runner-04    |    7 |   1.20% | 0 / 5              | ✅ OK           |
| perf-runner-07    |    2 |   0.00% | 0 / 0              | ⚪ INSUFFICIENT |
```

For each quarantine candidate, the report includes:
- The specific metrics it's noisy on (highest robust-z first — usually the leading indicator)
- First/last-seen timestamps so you can correlate with hardware or AZ changes
- A five-step remediation checklist: CPU governor, SMT, NUMA balancing, steal time, canary re-run

The remediation steps mirror the same runner-hardening controls the base performance gate depends on. If a supposedly-hardened runner still fails them, the runner has drifted from its provisioning template.

## Detection tuning

All knobs live under `runner_variance:` in `config/performance-budget.yaml`:

```yaml
runner_variance:
  window_days: 7
  min_runs_per_runner: 3
  min_samples_per_cell: 10
  robust_z_threshold: 3.5
  quarantine_metric_fraction: 0.30
  watch_metric_fraction: 0.15
```

Common tuning:
- **Small fleet (<5 runners)**: MAD becomes unstable. Raise `robust_z_threshold` to 4.5 or fall back to a fleet-wide absolute CV target (e.g. anything >3× your best runner's CV is suspect).
- **Very homogeneous bare-metal fleet**: lower `robust_z_threshold` to 2.5 to catch smaller regressions in noise.
- **Bootstrapping a new pool**: raise `min_runs_per_runner` to 5 so a hot new host doesn't get quarantined off two bad rolls of the dice.

## What to do after a quarantine

1. Remove the runner's `otel-perf-runner` label so the nightly pipeline stops targeting it.
2. Re-run the standalone canary workload (`telemetrygen traces --rate 1000 --duration 60s`) directly on the box.
3. If canary is stable, the host is fine — the noise came from a specific workload. Compare processor/OTTL config on that runner against the reference.
4. If canary is unstable, apply the [low-noise EC2 recipe](https://dev.to/kienmarkdo/low-noise-ec2-benchmarking-a-practical-guide-19f0): performance governor, disable SMT, disable NUMA balancing, dedicated tenancy.
5. Reintroduce with the label restored and observe the next weekly variance report.

## Design references

- Iglewicz & Hoaglin, *How to Detect and Handle Outliers* (ASQC 1993) — the modified z-score
- [Codspeed: Benchmarks in CI without noise](https://codspeed.io/blog/benchmarks-in-ci-without-noise) — CV → false-positive-rate math motivating per-runner isolation
- [Low-Noise EC2 Benchmarking (2026)](https://dev.to/kienmarkdo/low-noise-ec2-benchmarking-a-practical-guide-19f0) — remediation steps in the report checklist
- [Nethercote / rustls #1485](https://github.com/rustls/rustls/issues/1485) — argument for CV-adaptive rather than fixed thresholds
