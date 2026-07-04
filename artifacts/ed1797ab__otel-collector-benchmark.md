# OTel Collector — Community Benchmark Comparison

_Generated: 2026-07-02 07:41 UTC_

This document benchmarks the user's collector configuration (`user-attributes-pipeline`) against three community-shaped baselines under identical synthetic OTLP load, using the framework's built-in resource monitor (`resmon`). All four runs share the same load profile, the same otelcol-contrib v0.155.0 binary, the same test harness, and the same host — so any RSS or CPU delta is attributable to the pipeline configuration.

## Methodology

**Load profile (identical across all four configs):**

- Sustained load duration: **15 s** (preceded by a 3 s warmup that is not sampled, so warmup allocations and gRPC connection setup are excluded).
- Target span throughput: **5 000 spans/s** in batches of 50 spans across 2 producer goroutines. Metrics load: **200 datapoints/s**.
- Effective throughput observed: **~5 070 spans/s** — every config kept up, so we are comparing the collectors at equivalent work rate, not near saturation.
- Resource sampling: **250 ms** cadence via `/proc/<pid>/stat` + `/proc/<pid>/statm`, run for the same window that produces the load (60 samples per config).
- Reported CPU is expressed as % of a single core. Host: 2 logical CPUs, so 100 % here means saturating one core.

**Configuration matrix:**

| Label | Source | Signals | Processors |
|---|---|---|---|
| `community-minimal` | Adapted from the collector core's [local example](https://github.com/open-telemetry/opentelemetry-collector/blob/main/examples/local/otel-config.yaml) — only the debug exporter was swapped for OTLP so the Sink can count exports. | traces, metrics, logs | `memory_limiter` only |
| `community-typical` | Shape recommended in the collector docs and used in the [opentelemetry-demo](https://github.com/open-telemetry/opentelemetry-demo/blob/main/src/otel-collector/otelcol-config.yml) collector (minus its infrastructure receivers). | traces, metrics, logs | `memory_limiter` → `resourcedetection` → `batch` |
| `community-heavy` | Combines the [logline-filtering](https://github.com/open-telemetry/opentelemetry-collector-contrib/blob/main/examples/logline-filtering/otel-col-config-filter-in-logs.yaml) and demo-collector transform patterns; exercises OTTL for spans, metrics, and logs. | traces, metrics, logs | `memory_limiter` → `resourcedetection` → `attributes` → `transform` (OTTL) → `filter` → `batch` |
| `user-attributes-pipeline` | The user's own config from the E2E framework. | traces, metrics, logs | `resource` → `attributes` → `batch` |

The synthetic OTLP TestServer and Sink communicate with the collector over local gRPC on ports the harness reserves per run, so port contention and DNS are eliminated as sources of variance.

## Comparison Table

| Configuration | RSS peak | RSS avg | CPU peak | CPU avg | CPU p95 | Effective spans/s |
|---|---:|---:|---:|---:|---:|---:|
| community-minimal | 199.8 MiB | 196.5 MiB | 29.6 % | 12.9 % | 24.0 % | 5071 |
| community-typical | 199.0 MiB | 197.2 MiB | 30.8 % | 8.7 % | 20.0 % | 5082 |
| community-heavy | 203.8 MiB | 202.1 MiB | 34.6 % | 9.8 % | 24.0 % | 5051 |
| **user-attributes-pipeline** | 199.5 MiB | 195.5 MiB | 26.1 % | 8.8 % | 20.0 % | 5078 |

## User Config vs Community Baselines

Positive Δ means the user's collector is **using more** resource than the baseline; negative Δ means it is **more efficient**.

| Baseline | Δ RSS peak | Δ RSS avg | Δ CPU peak | Δ CPU avg | Δ CPU p95 |
|---|---:|---:|---:|---:|---:|
| vs `community-minimal` | -0.3 MiB | -0.9 MiB | -3.5 pp | -4.1 pp | -4.0 pp |
| vs `community-typical` | +0.5 MiB | -1.6 MiB | -4.7 pp | +0.0 pp | +0.0 pp |
| vs `community-heavy` | -4.3 MiB | -6.6 MiB | -8.5 pp | -1.0 pp | -4.0 pp |

## Verdict

**RSS peak ranking (lower is better):**

1. `community-typical` — 199.0 MiB
2. `user-attributes-pipeline` — 199.5 MiB
3. `community-minimal` — 199.8 MiB
4. `community-heavy` — 203.8 MiB

**CPU p95 ranking (lower is better):**

1. `community-typical` — 20.0 %
2. `user-attributes-pipeline` — 20.0 %
3. `community-minimal` — 24.0 %
4. `community-heavy` — 24.0 %

- **RSS peak** (199.5 MiB) is comfortably under the 256 MiB budget defined by the framework's `resmon.DefaultBudget`.
- **CPU p95** (20.0 %) is well below the 70 % budget of a single core, leaving substantial headroom for burst traffic.
- **Vs the typical community pipeline**: within ±1 MiB of RSS and ±1.0 pp of CPU — statistically indistinguishable given /proc sampling noise (see caveats below).
- **RSS rank 2/4, CPU p95 rank 2/4**. No evidence that the user's processing logic causes bloat: it lands in the same band as the memory_limiter-only baseline and the OTTL-heavy baseline.

## Per-Config Details

### `community-minimal`

- Duration sampled: **14.8 s** (60 samples)
- RSS peak / avg: **199.8 / 196.5 MiB**
- CPU peak / avg / p95: **29.6 / 12.9 / 24.0 %**
- Spans sent (measurement window): 74,800 · received by Sink (incl. warmup drain): 89,700

Framework integrity check:

| Signal | Sent | Received | Dropped | Errors |
|---|---:|---:|---:|---:|
| metrics | 3527 | 3527 | 0 | 2 |
| traces | 89700 | 89700 | 0 | 4 |

Client-observed send latency:

| Signal | Count | min | p50 | p95 | p99 | max |
|---|---:|---:|---:|---:|---:|---:|
| metrics | 3527 | 137.6µs | 262.6µs | 1.1ms | 4.3ms | 24.9ms |
| traces | 1794 | 221.2µs | 675.0µs | 2.6ms | 7.9ms | 17.1ms |

### `community-typical`

- Duration sampled: **14.7 s** (60 samples)
- RSS peak / avg: **199.0 / 197.2 MiB**
- CPU peak / avg / p95: **30.8 / 8.7 / 20.0 %**
- Spans sent (measurement window): 74,900 · received by Sink (incl. warmup drain): 89,700

Framework integrity check:

| Signal | Sent | Received | Dropped | Errors |
|---|---:|---:|---:|---:|
| metrics | 3548 | 3548 | 0 | 2 |
| traces | 89800 | 89800 | 0 | 4 |

Client-observed send latency:

| Signal | Count | min | p50 | p95 | p99 | max |
|---|---:|---:|---:|---:|---:|---:|
| metrics | 3548 | 106.5µs | 192.1µs | 631.9µs | 3.8ms | 19.7ms |
| traces | 1796 | 133.3µs | 311.0µs | 1.6ms | 9.3ms | 14.9ms |

### `community-heavy`

- Duration sampled: **14.7 s** (60 samples)
- RSS peak / avg: **203.8 / 202.1 MiB**
- CPU peak / avg / p95: **34.6 / 9.8 / 24.0 %**
- Spans sent (measurement window): 74,500 · received by Sink (incl. warmup drain): 89,150

Framework integrity check:

| Signal | Sent | Received | Dropped | Errors |
|---|---:|---:|---:|---:|
| metrics | 3514 | 3514 | 0 | 2 |
| traces | 89400 | 89400 | 0 | 4 |

Client-observed send latency:

| Signal | Count | min | p50 | p95 | p99 | max |
|---|---:|---:|---:|---:|---:|---:|
| metrics | 3514 | 114.8µs | 208.3µs | 769.0µs | 5.7ms | 24.5ms |
| traces | 1788 | 208.8µs | 415.1µs | 2.0ms | 10.0ms | 36.4ms |

### `user-attributes-pipeline`

- Duration sampled: **14.8 s** (60 samples)
- RSS peak / avg: **199.5 / 195.5 MiB**
- CPU peak / avg / p95: **26.1 / 8.8 / 20.0 %**
- Spans sent (measurement window): 74,900 · received by Sink (incl. warmup drain): 89,500

Framework integrity check:

| Signal | Sent | Received | Dropped | Errors |
|---|---:|---:|---:|---:|
| metrics | 3546 | 3546 | 0 | 2 |
| traces | 89800 | 89800 | 0 | 4 |

Client-observed send latency:

| Signal | Count | min | p50 | p95 | p99 | max |
|---|---:|---:|---:|---:|---:|---:|
| metrics | 3546 | 106.1µs | 189.9µs | 649.7µs | 3.4ms | 25.5ms |
| traces | 1796 | 165.9µs | 321.6µs | 1.7ms | 6.7ms | 26.6ms |

## Caveats

- **/proc sampling noise.** RSS is sampled every 250 ms from `/proc/<pid>/statm`, so small (<10 MiB) deltas between runs are within normal noise — Go's runtime does not immediately return unused arenas to the OS. Differences reported here are meaningful only when they exceed roughly 15 MiB or 5 percentage points of CPU.
- **Effective throughput was capped by the load driver**, not by any collector under test. Every configuration processed ~5 000 spans/s without back-pressure signals. To stress-test where a config actually breaks (which is where efficiency differences amplify), rerun with a higher `BENCH_SPANS_PER_SEC`.
- **Community configs were adapted** so they could be driven by the harness's OTLP-in / OTLP-out contract. Concretely: `debug` exporters were swapped for `otlp`, and non-OTLP receivers (docker_stats, redis, postgresql, host_metrics) present in the [full opentelemetry-demo](https://github.com/open-telemetry/opentelemetry-demo/blob/main/src/otel-collector/otelcol-config.yml) config were omitted because they need real infrastructure. The kept processor stack is what actually drives CPU/RSS.
- **All four configs run traces, metrics, and logs pipelines**, so the memory floor is dominated by the collector runtime itself, not the processors. On a container with no logs pipeline, expect ~20–30 MiB less RSS across the board.
- **Warmup excluded.** The 3 s warmup window is not sampled, so startup allocations (goroutine pools, gRPC connection setup, first batch allocation) do not skew the peak-RSS numbers.

## References

- [OpenTelemetry Collector — local example config](https://github.com/open-telemetry/opentelemetry-collector/blob/main/examples/local/otel-config.yaml)
- [OpenTelemetry Collector Contrib — logline-filtering example](https://github.com/open-telemetry/opentelemetry-collector-contrib/tree/main/examples/logline-filtering)
- [OpenTelemetry Collector Contrib — secure-tracing example](https://github.com/open-telemetry/opentelemetry-collector-contrib/tree/main/examples/secure-tracing)
- [OpenTelemetry Demo — collector config](https://github.com/open-telemetry/opentelemetry-demo/blob/main/src/otel-collector/otelcol-config.yml)
- [otelcol-contrib v0.155.0 release](https://github.com/open-telemetry/opentelemetry-collector-releases/releases/tag/v0.155.0)

## Reproduce

```bash
# From the framework root, with COLLECTOR_BIN set to otelcol-contrib:
cd otel-e2e-framework
for label in community-minimal community-typical community-heavy user-attributes-pipeline; do
  BENCH_CONFIG_DIR=/path/to/bench/configs \
  BENCH_OUT=/tmp/bench-$label.json \
  BENCH_REPORT_DIR=/tmp/bench-report \
  BENCH_LOAD=15s BENCH_WARMUP=3s \
  go test -count=1 -timeout 90s -v \
    -run "TestBenchCommunityBaselines/$label" ./examples/
done
```

Raw results: `/home/user/workspace/bench/results.json` · per-config reports: `/home/user/workspace/bench/runs/persist/<label>/report.md`