# Environmental factor correlation analysis

`scripts/analyze_environmental_factors.py` joins your S3 scorecard history with per-runner cloud metadata (instance type, region, AZ, tenancy, CPU generation, hyperthreading, governor, hour-of-day, day-of-week, background tasks) and ranks which factors most destabilize your nightly perf budget.

## Why this exists

`analyze_runner_variance.py` tells you *which runners* are noisy. This tool tells you *why* — is it a specific AZ, a smaller instance size, shared tenancy, SMT-on, an ondemand governor, or a cron job that fires during your bench window? Without this, "quarantine perf-runner-06" is a symptom fix; with it, you can rewrite your Terraform to avoid the pattern globally.

## Inputs

1. **Scorecard history** — directory of `scorecard-*.json` files (recursive). Each must include:
   - `started_at` (RFC-3339) — used for hour/day bucketing
   - `runner.name` — used as the join key to metadata
   - Metric samples under `metrics.<name>.samples[]` (or falls back to `current.value`)

2. **Runner metadata YAML** — see `config/runner_metadata.example.yaml`. One entry per runner. Populate from EC2 tags or IMDS during provisioning.

## Usage

```bash
python scripts/analyze_environmental_factors.py \
  --input-dir s3://my-bucket/scorecards/ \
  --metadata config/runner_metadata.yaml \
  --window-days 30 \
  --target-metrics e2e_p99_latency_ms memory_mib_per_kspans
```

Outputs:
- `reports/environmental/environmental_factors.md` — human-readable ranking + recommendations
- `reports/environmental/environmental_factors.json` — machine-readable, safe to diff or feed into Grafana annotations

## What each column means

| Column | Meaning |
|---|---|
| **eta²** | Fraction of variance in the metric explained by the factor. Cohen's classification: 0.01 small, 0.06 medium, 0.14 large. |
| **Levene p** | p-value for equal variance across levels (median-centered, robust). p < α (default 0.01) ⇒ heteroscedastic ⇒ this factor breaks CV-adaptive thresholds. |
| **Kruskal p** | Non-parametric equal-median test. Robust to the non-normal distributions typical of benchmarks. |
| **Hetero** | ✔ if Levene p < α. Heteroscedastic factors get their impact score multiplied by 1.5. |
| **Impact score** | `eta² × (1.5 if heteroscedastic else 1.0)`. Higher = optimize this first. |

## Reading the recommendations

The recommendation strings are factor-specific and actionable — for example:

- `tenancy`: migrate to `dedicated` — noisy-neighbor is the classic shared-tenancy failure mode.
- `hyperthreading`: disable SMT — sibling threads on the same physical core cause per-benchmark interference.
- `cpu_governor`: set to `performance` — `ondemand` and `powersave` create ~2-5% frequency-scaling jitter.
- `hour_bucket`: reschedule your nightly run — if benches at 06:00 UTC show higher variance, they likely overlap with a cron job or an AWS maintenance window.
- `background_tasks`: quiesce the co-tenant workload during the bench window (`systemctl stop`, cgroup freeze, etc.).

## Interpreting for your fleet

- **Impact score > 0.14** — this factor alone explains a large chunk of your noise. Fix before touching anything else.
- **Impact score 0.06-0.14** — meaningful contribution; fix once the large ones are handled.
- **Kruskal p > α but Levene p < α** — the *means* are the same across levels but the *spreads* are not. That's still bad for your adaptive gate, because the CV inflates on the noisier levels and lets real regressions through unnoticed.

## Extending

- Add new factors: edit the `FACTOR_EXTRACTORS` dict in `analyze_environmental_factors.py`.
- Change bucketing: adjust `environmental_analysis.time_of_day_buckets` in the metadata YAML.
- Add per-metric target lists: pass `--target-metrics` or edit `environmental_analysis.target_metrics`.

## Prior art

- Levene's median-centered test (Brown-Forsythe, 1974) — robust to non-normal distributions.
- Cohen (1988), *Statistical Power Analysis*, for the eta² thresholds.
- [Codspeed's 2025 CI noise study](https://codspeed.io/blog/benchmarks-in-ci-without-noise) confirmed that instance type and tenancy dominate CV on GitHub-hosted runners.
- [Heisler's cloud benchmarking guide](https://bheisler.github.io/post/benchmarking-in-the-cloud/) documents the SMT and governor effects this tool surfaces.
