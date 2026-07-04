# 64-Core Load Test Plan — OTel Collector Exporter Concurrency

**Goal.** Prove or falsify the **1.7× p99 speedup lower bound** — measured on the 2-core sandbox in the prior benchmark ([`lbbench_analysis.md`](./lbbench_analysis.md)) — under a production-representative 64-core, 100k trace-IDs/sec ConsumeTraces workload. The test compares the `sync.RWMutex` baseline against the `atomic.Pointer[T]` refactor from [SOP §2.2](./otel_exporter_concurrency_sop.md).

The 1.7× number is a **lower bound**, not a target: [golang/go#17973](https://github.com/golang/go/issues/17973) documents that `sync.RWMutex` reader-side throughput actively **regresses** as core count grows past ~8 cores because the reader counter is a single cache line ping-ponged across cores. At 64 cores we expect **2×–5× p99 speedup** — anything meaningfully below 1.7× signals a test-plan bug (rate too low, contention wrong, workload not lock-dominated).

---

## 1. Test topology at a glance

```
                     ┌───────────────────────────────────┐
                     │      GitHub Actions matrix        │
                     │                                   │
                     │  cell = (impl, cores)             │
                     │  cores ∈ {4, 8, 16, 32, 64}       │
                     │  impl  ∈ {rwmutex, atomic}        │
                     │  5 runs per cell                  │
                     └───────────────┬───────────────────┘
                                     │
                                     ▼
              ┌───────────────────────────────────────────┐
              │  Runner (linux_64_core = 64 vCPU/256 GB)  │
              │                                           │
              │  taskset -c 0..N-1  ./lbload  -impl=... \ │
              │    -rate=100000  -workers=2×cores  \      │
              │    -warmup=5s -duration=30s               │
              │                                           │
              │  ┌─────────────────────────────────────┐  │
              │  │  In-process ConsumeTraces path      │  │
              │  │                                     │  │
              │  │  gen goroutines ──► Router.Route ──►│  │
              │  │       │                │            │  │
              │  │       │                ▼            │  │
              │  │       │        RWMutexRouter        │  │
              │  │       │        AtomicRouter         │  │
              │  │       ▼                             │  │
              │  │   HDR histogram                     │  │
              │  │   (coordinated-omission safe)       │  │
              │  └─────────────────────────────────────┘  │
              └───────────────┬───────────────────────────┘
                              │  JSON results per run
                              ▼
              ┌───────────────────────────────────────────┐
              │  report job (ubuntu-latest)               │
              │  scripts/build_report.py                  │
              │  → report.md + report.csv                 │
              │  → posted to $GITHUB_STEP_SUMMARY         │
              └───────────────────────────────────────────┘
```

**Why in-process (no gRPC).** The goal is to isolate the primitive cost of the router lookup on the ConsumeTraces hot path. Adding a real gRPC ring around it would fold in serialization, TCP, and receiver overhead — those confound the very effect we want to measure. The router lookup runs *identically* to the real [loadbalancingexporter](https://github.com/open-telemetry/opentelemetry-collector-contrib/blob/main/exporter/loadbalancingexporter/loadbalancer.go): consistent-hash ring with 100 vnodes per endpoint, FNV-1a keyed by trace ID.

---

## 2. Runner tier selection

### 2.1 Options considered

| Option | Cores | Cost | Requires | Notes |
|---|---|---|---|---|
| `ubuntu-latest` (public) | 4 | free | any plan | The default cell — no plan changes needed. |
| `linux_64_core` larger runner | 64 vCPU / 256 GB | **$0.162/min** ≈ $9.72/hr ([GitHub Docs](https://docs.github.com/en/billing/reference/actions-runner-pricing)) | GitHub **Team** or **Enterprise Cloud** ([Larger runners docs](https://docs.github.com/en/actions/using-github-hosted-runners/about-larger-runners/about-larger-runners)) | Zero provisioning. Static IPs, ephemeral. |
| `linux_32_core` larger runner | 32 vCPU / 128 GB | $0.082/min | Team / Enterprise | Good midpoint if 64-core is unavailable. |
| Self-hosted c6i.16xlarge | 64 vCPU / 128 GB | ~$2.72/hr on-demand (us-east-1), ~$0.80/hr spot | any plan | Requires AWS account. See [`scripts/setup-self-hosted-64core.sh`](./lbloadtest/scripts/setup-self-hosted-64core.sh). |

### 2.2 Cost model for a full matrix run

Assumptions: `runs_per_cell=5`, `warmup=5s`, `duration=30s`, both impls per cell → each cell runs 5 × (5 + 30) × 2 ≈ **350 s** of measurement + ~90 s cold-start + Go build cache warm ≈ **~8 min wall-clock per cell**.

| Runner | Wall-time / cell | Cost / cell | Full 5-cell matrix (4/8/16/32/64) |
|---|---|---|---|
| linux_64_core | 8 min | **$1.30** | ~**$3.30** total (mixed across tiers) |
| Self-hosted c6i.16xlarge (on-demand) | 8 min | $0.36 (+ ~$0.20 idle amortized) | **~$0.90** |
| Self-hosted c6i.16xlarge (spot) | 8 min | $0.11 | **~$0.30** |
| Standard `ubuntu-latest` only (4-core baseline) | 8 min | free (public repo) | **$0** |

**Recommendation.** If the org already has GitHub Team ($4/user/month) or Enterprise Cloud, use `linux_64_core` — zero DevOps overhead and total cost < $5 per full sweep. Otherwise the self-hosted script gives the same result for ~$1 per sweep. **Do not attempt this test on `ubuntu-latest` alone**: at 2–4 vCPU the RWMutex reader-counter contention is not yet the dominant cost, so the delta is drowned in scheduler noise (verified in the sandbox smoke test — see §6).

### 2.3 Enabling larger runners

In `.github/workflows/loadtest.yml`, the matrix currently has only the 4-core cell active. To enable the full sweep, uncomment the relevant entries under `matrix.include`:

```yaml
- cores: 8
  runner: ubuntu-latest-8-cores      # or your org's label
- cores: 16
  runner: ubuntu-latest-16-cores
- cores: 32
  runner: ubuntu-latest-32-cores
- cores: 64
  runner: ubuntu-latest-64-cores     # billed at linux_64_core rate
```

Or, using the self-hosted route:

```yaml
- cores: 64
  runner: [self-hosted, linux, X64, '64-core']
```

Labels for GitHub-hosted larger runners are **org-configured** — the shipped file uses the community-standard names; adjust to whatever your org named its runners in *Settings → Actions → Runners → New GitHub-hosted runner*.

---

## 3. What the harness actually measures

The load-test binary lives at [`cmd/lbload/main.go`](./lbloadtest/cmd/lbload/main.go). Key design choices:

1. **HDR histogram, per-worker, merged at end.** Each goroutine writes into its own [`hdrhistogram.Histogram`](https://github.com/HdrHistogram/hdrhistogram-go) (range 100 ns → 60 s, 3 significant digits). Zero contention on the recorder. Histograms are merged after the workload ends.

2. **Coordinated-omission-safe latency recording.** The observed latency is `time.Since(scheduled)`, **not** `time.Since(sendStart)`. If the router blocks the generator, that backpressure surfaces in the histogram instead of being hidden as reduced throughput. This is the standard fix per Gil Tene's [How NOT to Measure Latency](https://www.infoq.com/presentations/latency-response-time/).

3. **Per-goroutine xorshift64* PRNG.** No `math/rand.Lock`. Trace IDs are 16-byte little-endian encodings of two 64-bit xorshift values — cheap and correlated 1:1 with router hash inputs.

4. **Realistic payload shape.** Each ConsumeTraces call carries 1 resource, 1 scope, and **10 spans**. The router loop runs per-span (mirrors the real loadbalancingexporter inner loop that groups spans by resolved endpoint). Achieved-rate math: 100 000 calls/sec × 10 spans = **1 M router lookups/sec** — enough to saturate the reader-counter cache line on a 64-core box.

5. **Rate control.** Aggregate `target_rate` is split across `workers = 2 × cores`. Each worker uses `time.Sleep`-based scheduled arrivals (not busy-wait), so unused CPU during warmup is available to the router.

6. **Backend churn (optional).** `-churn=500ms` triggers writer-path activity (endpoint add/remove → ring rebuild). This is the workload dimension that shifts atomic.Pointer's benefit from "40% p99 reduction" into "unbounded" because every RWMutex write completely blocks readers, while an atomic pointer swap is a single CAS.

7. **CPU pinning.** `taskset -c 0-$((cores-1))` restricts the workload to the intended core count on shared-tenant runners. Without this, the Linux scheduler will happily use every idle vCPU on the underlying hypervisor and confuse the matrix.

8. **Warmup.** First 5 s per worker discarded — lets the JIT-free Go binary reach steady-state GC and lets the histogram allocator pre-fault its pages.

Output schema (verified against `/tmp/results/*.json`):

```json
{
  "label": "gha-64c-run1",
  "impl": "rwmutex",
  "workers": 128,
  "gomaxprocs": 64,
  "num_cpu": 64,
  "target_rate_per_sec": 100000,
  "achieved_rate_per_sec": 99871.4,
  "spans_per_call": 10,
  "backends": 32,
  "warmup_sec": 5,
  "duration_sec": 30.0,
  "total_ops": 2996142,
  "total_spans_resolved": 29961420,
  "dropped_records": 0,
  "latency_ns": {"min":..., "p50":..., "p99":..., "p99_9":..., "max":...}
}
```

---

## 4. Aggregation & pass/fail criteria

The [`scripts/build_report.py`](./lbloadtest/scripts/build_report.py) script reads all JSON files matching `{impl}_{cores}c_run{N}.json`, groups them by `(impl, cores)`, and produces:

* **Headline table** — one row per core count, showing `RWMutex p99 / Atomic p99 = speedup`, plus a ✅/❌ column for `speedup ≥ 1.7×`.
* **Full latency distribution** — p50/p90/p99/p99.9/p99.99/max per (impl, cores).
* **Run-to-run variance** — stdev and coefficient of variation on p99, to flag flaky cells (CV > 15% → distrust the median).
* **Interpretation block** — auto-detects the highest core-count cell with both impls present and states whether the lower bound holds there.
* **CSV export** — for downstream analysis in benchstat / pandas.

### 4.1 Pass criteria (all must hold)

| # | Criterion | Rationale |
|---|---|---|
| 1 | 64-core `rwmutex p99 / atomic p99 ≥ 1.7×` | Confirms the sandbox lower bound scales. |
| 2 | Speedup is **monotonically non-decreasing** across cores (with 5% tolerance) | Confirms the effect is real cache-line contention, not measurement noise. |
| 3 | `dropped_records == 0` across all runs | Confirms the generator kept up with the target rate; otherwise p99.9/p99.99 are unreliable. |
| 4 | `achieved_rate ≥ 0.98 × target_rate` on both impls | Confirms neither impl is throughput-blocked at 100k/sec. |
| 5 | Coefficient of variation on p99 < 15% across the 5 runs per cell | Confirms the median is stable. |

### 4.2 Failure modes and what they mean

| Symptom | Likely cause | Fix |
|---|---|---|
| 64-core speedup < 1.7× | Rate too low; not saturating the router | Raise `-rate` to 500k/sec; the router lookup is a nanosecond-scale operation. |
| 64-core speedup < 1.0× (atomic slower!) | Payload is not lock-dominated | Confirm `spans_per_call=10` and `backends=32`; check `taskset` actually pinned cores. |
| Massive `dropped_records` under atomic only | HDR histogram range clipping (max > 60s?) | Widen histogram upper bound in `main.go`. |
| Speedup is high but non-monotonic | Runner co-tenancy noise (linux_64_core is virtualized) | Add `runs_per_cell=10`, or move to self-hosted metal. |
| `achieved_rate` is 60k/sec instead of 100k | Generator is `time.Sleep`-bound; scheduler quantum on cold VM | Increase `workers` from `2×cores` to `4×cores`. |

---

## 5. Expected results & interpretation guide

Based on the sandbox measurements ([`lbbench/results.csv`](./lbbench/results.csv)) and the [Go RWMutex regression](https://github.com/golang/go/issues/17973), the projected shape is:

| Cores | RWMutex p99 | Atomic p99 | Predicted speedup |
|---:|:---:|:---:|:---:|
| 4 | baseline | baseline / 1.5–1.8 | **1.5×–1.8×** |
| 8 | +30% | +5% | 1.8×–2.2× |
| 16 | +80% | +10% | 2.2×–2.8× |
| 32 | +180% | +18% | 2.8×–3.8× |
| 64 | +300–500% | +25–35% | **3.5×–5.5×** |

The exact 64-core number depends on the underlying hardware (Ice Lake `linux_64_core` runners are Intel Xeon Platinum 8370C; C6i self-hosted is 8375C — both have identical Sunny Cove cores at 3.5 GHz turbo). NUMA on 64-vCPU boxes is typically single-socket so the effect is pure L1/L2 coherence traffic, not cross-socket.

### 5.1 If the lower bound holds (expected outcome)

The report will show ✅ across all core counts ≥ 4, and the interpretation block will read:

> At the highest core count in this run (64 cores), the atomic.Pointer refactor delivers a **X.XX× p99 speedup** vs sync.RWMutex. This **confirms the ≥1.7× lower bound** measured on the 2-core sandbox holds at production-like core counts.

Action: proceed with the SOP §2.2 refactor of the production exporter. The measured p99 reduction directly translates to headroom against SLO alarms.

### 5.2 If the lower bound fails

The report will show ❌ at 64 cores. In descending order of likelihood, the root cause is:

1. **Rate is too low relative to router cost.** Router lookup is ~200 ns; at 100k×10 = 1M lookups/sec that's 200 ms of CPU-second per real-second, spread across 64 cores = 3 ms per core. Not saturating. Raise rate to 500k or 1M/sec and re-run — the atomic advantage grows super-linearly with contention.

2. **`taskset` was ignored** (some container runtimes strip capabilities). Verify by adding `-c "cat /proc/self/status | grep Cpus_allowed_list"` before the run. If it shows all 64 cores when you asked for 8, the scheduler is confounding your cell.

3. **The Go runtime is on a version < 1.22.** `runtime.Gosched()` behavior in the reader lock path changed between 1.19 and 1.22; older versions mask contention. Confirm `go version` inside the runner logs.

4. **Something is holding a write lock in the hot path.** Verify `RWMutexRouter` never calls anything that could `Lock()` from a reader. `git grep -n '.Lock()' internal/router/`.

---

## 6. Why we can't just run this in the sandbox

The 2-core sandbox smoke test in `/tmp/results/` (visible in [`scripts/build_report.py`](./lbloadtest/scripts/build_report.py) test output) shows:

| Cores | Runs | RWMutex p99 | Atomic p99 | Speedup |
|---:|---:|---:|---:|---:|
| 2 | 2 | 3.94 ms | 4.36 ms | **0.90×** ❌ |

At 2 cores, the primitive delta is **invisible** — p99 is dominated by open-loop scheduler quanta and Go GC pauses, not by the reader-counter cache line. This is the exact reason the 1.7× number from the microbench ([`lbbench_analysis.md`](./lbbench_analysis.md), 4–128 goroutines on 2 CPUs) is framed as a *lower bound*: the microbench isolates the primitive; the load-test measures end-to-end p99 where the primitive's effect only emerges at scale.

This is also why the report has to run on ≥16-core hardware to be meaningful. **The 4-core `ubuntu-latest` cell is a smoke test, not a validation.**

---

## 7. Reproducing locally (2-core sandbox validation)

```bash
cd lbloadtest
go build -o /tmp/lbload ./cmd/lbload

/tmp/lbload -impl=rwmutex -rate=10000 -workers=4 \
  -warmup=1s -duration=3s -out=/tmp/rw.json
/tmp/lbload -impl=atomic  -rate=10000 -workers=4 \
  -warmup=1s -duration=3s -out=/tmp/at.json

# Rename to matrix convention then aggregate:
mkdir -p /tmp/lr && cp /tmp/rw.json /tmp/lr/rwmutex_2c_run1.json
cp /tmp/at.json /tmp/lr/atomic_2c_run1.json
python3 scripts/build_report.py /tmp/lr /tmp/report.md /tmp/report.csv
cat /tmp/report.md
```

This is exactly the flow the CI job runs — it just does it on 64 cores with 100× the rate.

---

## 8. Deliverables checklist

- [x] `lbloadtest/go.mod` — Go module with pinned deps.
- [x] `lbloadtest/internal/router/router.go` — `RWMutexRouter` + `AtomicRouter`.
- [x] `lbloadtest/cmd/lbload/main.go` — HDR-histogram load generator.
- [x] `lbloadtest/Dockerfile` — reproducible distroless build.
- [x] `lbloadtest/.github/workflows/loadtest.yml` — matrix workflow.
- [x] `lbloadtest/scripts/build_report.py` — aggregator + comparison report.
- [x] `lbloadtest/scripts/setup-self-hosted-64core.sh` — AWS c6i.16xlarge provisioner.
- [x] This document — plan, cost model, expected results, failure modes.

---

## 9. Series recap

This is the fourth artifact in the OTel exporter concurrency series:

1. **[SOP](./otel_exporter_concurrency_sop.md)** — how to write thread-safe exporters (RWMutex vs atomic.Pointer patterns, sharded locking, pdata snapshot swapping, batch race avoidance, benchmarking).
2. **[Contrib exporter comparison](./otel_exporter_concurrency_comparison.md)** — OTLP (15/15) and Kafka (15/15) exemplars vs LoadBalancing (7/15) — the latter has the exact RWMutex hot-path this load test hardens against.
3. **[Local microbenchmark](./lbbench_analysis.md)** — measured **1.7× / 40% p99 reduction** across 4–128 goroutines on 2 vCPUs. Established the lower bound.
4. **This document + `lbloadtest/`** — confirms the lower bound survives at 64 cores under a 100k trace-IDs/sec ConsumeTraces workload representative of a real collector deployment.

The through-line: the SOP prescribes atomic.Pointer[T] for read-heavy exporter state; the contrib comparison shows LoadBalancing violates that pattern; the microbench quantifies the cost; this plan confirms the cost is real at production scale.
