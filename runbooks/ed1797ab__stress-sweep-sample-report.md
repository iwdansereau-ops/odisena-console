# OTel Collector Pipeline E2E Report

- **Started**: 2026-07-02T08:16:52Z
- **Duration**: 31.38s

## Environment

- **rendered_config**: `/tmp/stress-pprof/rendered-config.yaml`

## Telemetry Integrity

| Signal | Sent | Received | Dropped | Send Errors |
|--------|-----:|---------:|--------:|------------:|
| traces | 612850 | 612850 | 0 | 16 |

## Pipeline Send Latency

Client-observed round-trip latency from TestServer → collector receiver.

| Signal | Count | min | p50 | p95 | p99 | max |
|--------|------:|----:|----:|----:|----:|----:|
| traces | 12257 | 136.6µs | 404.8µs | 1.7ms | 4.6ms | 29.1ms |

## Collector Stress Sweep

**Knee point**: none — the collector tripped a guardrail on the first step.  
**Budget**: RSS ≤ 180.0MiB · CPU p95 ≤ 90.0% · drop-rate ≤ 1.00% · send p95 ≤ 100.0ms

| Target sps | Achieved sps | Duration | Sent | Received | Drop% | RSS peak | CPU peak / p95 | Send p50 / p95 / p99 | Verdict |
|-----------:|-------------:|---------:|-----:|---------:|------:|---------:|---------------:|---------------------:|:--------|
| 5000 | 4999 | 6.0s | 30000 | 34300 | 0.00% | 197.0MiB | 12.0% / 8.0% | 492.2µs / 1.8ms / 4.5ms | ❌ (RSS 197.0MiB > 180.0MiB) |
| 10000 | 9966 | 6.0s | 59800 | 69300 | 0.00% | 200.9MiB | 16.0% / 8.0% | 406.9µs / 1.7ms / 6.6ms | ❌ (RSS 200.9MiB > 180.0MiB) |
| 25000 | 24806 | 6.0s | 148850 | 173250 | 0.00% | 210.5MiB | 44.0% / 20.0% | 406.2µs / 1.7ms / 4.0ms | ❌ (RSS 210.5MiB > 180.0MiB) |
| 50000 | 47524 | 6.0s | 285200 | 334950 | 0.00% | 211.2MiB | 44.0% / 28.0% | 382.2µs / 1.9ms / 5.0ms | ❌ (RSS 211.2MiB > 180.0MiB) |

**Charts**:

- ![latency-vs-throughput.png](latency-vs-throughput.png)
- ![rss-vs-throughput.png](rss-vs-throughput.png)

### Heap profile — step 1 (5000 spans/s, RSS 197.0MiB)

Raw profile: [`heap-step-1-5000sps.pb.gz`](heap-step-1-5000sps.pb.gz) — inspect with `go tool pprof <file>`.

**Top allocators by `inuse_space`:**

| Rank | Function | Inuse | Share |
|-----:|----------|------:|------:|
| 1 | `github.com/aws/aws-sdk-go/aws/endpoints.init` | 3.5MiB | 15.8% |
| 2 | `cloud.google.com/go/compute/apiv1/computepb.init` | 2.4MiB | 11.0% |
| 3 | `google.golang.org/grpc/internal/mem.(*SimpleBufferPool).Get` | 2.4MiB | 10.7% |
| 4 | `google.golang.org/protobuf/internal/filedesc.(*File).initDecls` | 2.3MiB | 10.3% |
| 5 | `google.golang.org/protobuf/reflect/protoregistry.(*Types).register` | 1.0MiB | 4.7% |


### Heap profile — step 2 (10000 spans/s, RSS 200.9MiB)

Raw profile: [`heap-step-2-10000sps.pb.gz`](heap-step-2-10000sps.pb.gz) — inspect with `go tool pprof <file>`.

**Top allocators by `inuse_space`:**

| Rank | Function | Inuse | Share |
|-----:|----------|------:|------:|
| 1 | `github.com/aws/aws-sdk-go/aws/endpoints.init` | 3.5MiB | 15.4% |
| 2 | `cloud.google.com/go/compute/apiv1/computepb.init` | 2.4MiB | 10.8% |
| 3 | `google.golang.org/grpc/internal/mem.(*SimpleBufferPool).Get` | 2.4MiB | 10.5% |
| 4 | `google.golang.org/protobuf/internal/filedesc.(*File).initDecls` | 2.3MiB | 10.1% |
| 5 | `google.golang.org/protobuf/reflect/protoregistry.(*Types).register` | 1.0MiB | 4.6% |


### Heap profile — step 3 (25000 spans/s, RSS 210.5MiB)

Raw profile: [`heap-step-3-25000sps.pb.gz`](heap-step-3-25000sps.pb.gz) — inspect with `go tool pprof <file>`.

**Top allocators by `inuse_space`:**

| Rank | Function | Inuse | Share |
|-----:|----------|------:|------:|
| 1 | `github.com/aws/aws-sdk-go/aws/endpoints.init` | 3.5MiB | 15.2% |
| 2 | `cloud.google.com/go/compute/apiv1/computepb.init` | 2.4MiB | 10.7% |
| 3 | `google.golang.org/grpc/internal/mem.(*SimpleBufferPool).Get` | 2.4MiB | 10.3% |
| 4 | `google.golang.org/protobuf/internal/filedesc.(*File).initDecls` | 2.3MiB | 9.9% |
| 5 | `google.golang.org/protobuf/reflect/protoregistry.(*Types).register` | 1.0MiB | 4.5% |


### Heap profile — step 4 (50000 spans/s, RSS 211.2MiB)

Raw profile: [`heap-step-4-50000sps.pb.gz`](heap-step-4-50000sps.pb.gz) — inspect with `go tool pprof <file>`.

**Top allocators by `inuse_space`:**

| Rank | Function | Inuse | Share |
|-----:|----------|------:|------:|
| 1 | `github.com/aws/aws-sdk-go/aws/endpoints.init` | 3.5MiB | 14.4% |
| 2 | `cloud.google.com/go/compute/apiv1/computepb.init` | 2.4MiB | 10.0% |
| 3 | `google.golang.org/protobuf/internal/filedesc.(*File).initDecls` | 2.3MiB | 9.4% |
| 4 | `compress/flate.NewWriter` | 1.8MiB | 7.2% |
| 5 | `google.golang.org/grpc/internal/mem.(*SimpleBufferPool).Get` | 1.6MiB | 6.5% |

