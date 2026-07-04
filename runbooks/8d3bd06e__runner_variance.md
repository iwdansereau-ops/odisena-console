# Runner Variance Report

**Window:** last 7 days &nbsp;·&nbsp; **Scorecards analyzed:** 44 &nbsp;·&nbsp; **Runners seen:** 7

**Summary:** 🔴 1 quarantine · 🟡 0 watch · ✅ 5 healthy · ⚪ 1 insufficient data

## Runners ranked by noise contribution

| Runner | Runs | Mean CV | High-noise metrics | Verdict |
|---|---:|---:|---:|:---:|
| `perf-runner-06` | 7 | 5.89% | 5 / 5 | 🔴 QUARANTINE |
| `perf-runner-04` | 7 | 1.20% | 0 / 5 | ✅ OK |
| `perf-runner-02` | 7 | 1.18% | 0 / 5 | ✅ OK |
| `perf-runner-05` | 7 | 1.02% | 0 / 5 | ✅ OK |
| `perf-runner-03` | 7 | 0.93% | 0 / 5 | ✅ OK |
| `perf-runner-01` | 7 | 0.87% | 0 / 5 | ✅ OK |
| `perf-runner-07` | 2 | 0.00% | 0 / 0 | ⚪ INSUFFICIENT_DATA |

## 🔴 Quarantine candidates

Recommend removing these runners from the `otel-perf-runner` label until root-caused.

### `perf-runner-06`
- Runs in window: **7** (2026-06-24T06:00:00Z → 2026-06-30T06:00:00Z)
- Mean CV across tracked metrics: **5.89%**
- High-noise metrics (5 of 5):
  - memory_mib_per_kspans (CV=6.25%, z=38.52)
  - cpu_cores_per_kspans (CV=5.76%, z=28.24)
  - sustained_throughput_sps (CV=6.20%, z=25.34)
  - e2e_p99_latency_ms (CV=6.14%, z=14.62)
  - alloc_bytes_per_span (CV=5.11%, z=9.62)

**Suggested remediation checklist:**
1. Confirm CPU governor and C-states pinned (`cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_governor` → `performance`).
2. Verify SMT/hyperthreading is disabled (`lscpu | grep 'Thread(s) per core'` → 1).
3. Check NUMA balancing is off (`cat /proc/sys/kernel/numa_balancing` → 0).
4. Inspect for noisy tenants (`grep -c ^processor /proc/cpuinfo` vs. instance vCPU spec; unexpected steal time in `mpstat 1 5`).
5. Reboot and re-run the canary workload from `otel-nightly-perf`; if canary regresses, the host is the problem, not the code.

## Fleet CV distribution (reference)

| Metric | Median CV | MAD | Min | Max | Runners |
|---|---:|---:|---:|---:|---:|
| alloc_bytes_per_span | 1.17% | 0.28% | 0.79% | 5.11% | 6 |
| cpu_cores_per_kspans | 1.04% | 0.11% | 0.81% | 5.76% | 6 |
| e2e_p99_latency_ms | 1.08% | 0.23% | 0.73% | 6.14% | 6 |
| memory_mib_per_kspans | 1.01% | 0.09% | 0.81% | 6.25% | 6 |
| sustained_throughput_sps | 1.07% | 0.14% | 0.86% | 6.20% | 6 |

## Methodology

- **CV** = stdev / |mean| × 100 over all samples a runner contributed for a given metric during the 7-day window.
- **Robust z-score** = 0.6745 × (CV − median_CV) / MAD (Iglewicz & Hoaglin). Threshold: **3.5**.
- **Quarantine** if a runner is flagged HIGH_NOISE on ≥ 30% of its tracked metrics.
- **Watch** if HIGH_NOISE fraction is between 15% and 30%.
- Cells with <3 runs or <10 pooled samples are marked INSUFFICIENT_DATA.
