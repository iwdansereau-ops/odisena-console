# OTel Collector Exporter Concurrency: Comparative Analysis

**Exporters analyzed** (source pulled from `main` on 2026-07-02):

| # | Exporter | Repo | Primary role |
|---|---|---|---|
| 1 | **OTLP** | `opentelemetry-collector/exporter/otlpexporter` | gRPC dispatch to a fixed backend |
| 2 | **Kafka** | `opentelemetry-collector-contrib/exporter/kafkaexporter` | Async publish to Kafka topics (franz-go) |
| 3 | **Load Balancing** | `opentelemetry-collector-contrib/exporter/loadbalancingexporter` | Dynamic backend resolution + consistent-hash routing |

The three cover the spectrum of state-management complexity: OTLP's state is effectively immutable-after-`Start`; Kafka carries a pooled buffer allocator; loadbalancing rebuilds a routing table at runtime under lock. That range makes the comparison against the SOP concrete.

---

## 1. Per-exporter concurrency dossier

### 1.1 OTLP exporter — `baseExporter`

Source: [`exporter/otlpexporter/otlp.go`](https://github.com/open-telemetry/opentelemetry-collector/blob/main/exporter/otlpexporter/otlp.go)

```go
type baseExporter struct {
    config          *Config
    traceExporter   ptraceotlp.GRPCClient
    metricExporter  pmetricotlp.GRPCClient
    logExporter     plogotlp.GRPCClient
    profileExporter pprofileotlp.GRPCClient
    clientConn      *grpc.ClientConn
    metadata        metadata.MD
    callOptions     []grpc.CallOption
    settings        component.TelemetrySettings
    userAgent       string
}
```

**Concurrency primitives:** none. Zero occurrences of `sync.*`, `atomic.*`, `Mutex`, or `RWMutex` in the entire file.

**Model:** All fields are populated in `start()` and never mutated afterward. `Consume*` becomes:

```go
func (e *baseExporter) pushTraces(ctx context.Context, td ptrace.Traces) error {
    if e.traceExporter == nil { return errors.New("otlp exporter not started") }
    req := ptraceotlp.NewExportRequestFromTraces(td)
    resp, respErr := e.traceExporter.Export(ctx, req, e.callOptions...)
    // ...
}
```

Correctness rests on two guarantees: (a) `grpc.ClientConn` is documented safe for concurrent use, and (b) `Start` happens-before any `Consume*` per the Collector runtime — which is exactly the "one-shot publication" pattern from SOP §1 (`sync.Once` + plain field, or in this case, plain field because the Collector already provides the barrier).

Factory declares `MutatesData: false` for all four signals ([`factory.go`](https://github.com/open-telemetry/opentelemetry-collector/blob/main/exporter/otlpexporter/factory.go) L82/101/120/139), so no pdata cloning is required.

---

### 1.2 Kafka exporter — `kafkaExporter[T]`

Source: [`exporter/kafkaexporter/kafka_exporter.go`](https://github.com/open-telemetry/opentelemetry-collector-contrib/blob/main/exporter/kafkaexporter/kafka_exporter.go)

```go
type kafkaExporter[T any] struct {
    cfg          Config
    set          exporter.Settings
    tb           *metadata.TelemetryBuilder
    logger       *zap.Logger
    newMessenger func(host component.Host) (messenger[T], error)
    messenger    messenger[T]
    producer     *kafkaclient.FranzSyncProducer
    recordsPool  sync.Pool     // <-- only sync primitive
}

type recordsBuffer struct {
    space    []kgo.Record
    pointers []*kgo.Record
}
```

**Concurrency primitives:** exactly one — `sync.Pool` for reusing `recordsBuffer` allocations across parallel exports. This is not a mutual-exclusion primitive; it's a GC-pressure optimization that also happens to be goroutine-safe.

**Model:** All state is immutable after `Start`, plus one pooled allocator. The `franz-go` `*kgo.Client` (wrapped by `FranzSyncProducer`) is thread-safe for concurrent `ProduceSync` calls, so multiple `num_consumers` workers can push through a single producer without synchronization. `MutatesData: false` in [`factory.go`](https://github.com/open-telemetry/opentelemetry-collector-contrib/blob/main/exporter/kafkaexporter/factory.go) L173.

Interesting nuance: because a single input batch fans out to multiple Kafka messages per partition key (via `batchpersignal`), each ResourceSpans/Log/Metric is `CopyTo`'d into a fresh pdata instance before marshaling — 4 `CopyTo` sites in the file, one per signal. This matches SOP §4.1 (retain past `Consume*` return → clone).

---

### 1.3 Load Balancing exporter — `loadBalancer` + resolvers

Sources: [`loadbalancer.go`](https://github.com/open-telemetry/opentelemetry-collector-contrib/blob/main/exporter/loadbalancingexporter/loadbalancer.go), [`trace_exporter.go`](https://github.com/open-telemetry/opentelemetry-collector-contrib/blob/main/exporter/loadbalancingexporter/trace_exporter.go), [`resolver_k8s.go`](https://github.com/open-telemetry/opentelemetry-collector-contrib/blob/main/exporter/loadbalancingexporter/resolver_k8s.go)

```go
type loadBalancer struct {
    logger              *zap.Logger
    host                component.Host
    res                 resolver
    ring                *hashRing
    componentFactory    componentFactory
    exporters           map[string]*wrappedExporter
    exportersShutdownWG sync.WaitGroup
    stopped             bool
    updateLock          sync.RWMutex           // guards ring + exporters
}
```

**Concurrency primitives:** `sync.RWMutex` (routing), `sync.WaitGroup` (shutdown), plus in resolvers: `sync.Map` (K8s endpoints store), a second `sync.RWMutex` for change-callback fan-out, and `sync.Once` for one-shot callback triggering.

**Hot-path pattern:**

```go
func (lb *loadBalancer) exporterAndEndpoint(identifier []byte) (*wrappedExporter, string, error) {
    lb.updateLock.RLock()
    defer lb.updateLock.RUnlock()
    endpoint := lb.ring.endpointFor(identifier)
    exp, found := lb.exporters[endpointWithPort(endpoint)]
    // ...
}
```

`ConsumeTraces` calls `exporterAndEndpoint` **once per unique trace ID in the batch** (a per-batch `expByTID` map amortizes duplicates). At 4 `num_consumers` × batches averaging N unique trace IDs, this is 4·N `RLock` acquisitions per second — precisely the workload where SOP §2.1 warns RWMutex begins to cache-line-thrash past ~8 concurrent readers ([golang/go#17973](https://github.com/golang/go/issues/17973)).

**Documented race window:** the file itself comments (loadbalancer.go L228–230):

> "make rolling updates of next tier of collectors work. currently, this may cause data loss because the latest batches sent to outdated backend will never find their way out. for details: [issue #1690](https://github.com/open-telemetry/opentelemetry-collector-contrib/issues/1690)"

This is a live TOCTOU-shaped issue: an in-flight batch resolves an endpoint, then `onBackendChanges` swaps the ring and shuts down the old backend before the batch drains. The `RLock`/`WLock` protects the *lookup*, not the *lifetime* of the returned `*wrappedExporter`. It's a real gap in the design that an atomic-pointer-to-immutable-snapshot approach doesn't automatically fix — you'd still need refcounting or a graceful-drain phase.

`MutatesData: false` on all three signal exporters. Fan-out copies aggressively — 22 `CopyTo` call sites across the three signal implementations, because each unique routing key needs its own owned pdata tree.

---

## 2. Direct mapping against the SOP

The SOP prescribes a preference order: **atomic scalar → `atomic.Pointer[T]` → `sync.RWMutex` → sharded RWMutex** for read-heavy state, plus explicit rules on pdata mutation, retention, and shutdown.

| SOP requirement | OTLP | Kafka | Load Balancing |
|---|---|---|---|
| **§1** `Capabilities().MutatesData` correctly declared | ✅ `false` (all signals) | ✅ `false` | ✅ `false` |
| **§1** Publish-once state uses no primitive | ✅ Plain fields, Start→Consume barrier | ✅ Plain fields | ⚠️ Mostly, but `ring` + `exporters` are runtime-mutable |
| **§2.1** RWMutex only where cross-field invariants required | N/A | N/A | ⚠️ RWMutex used, but state is a *whole-object swap* (ring + map together) — a textbook `atomic.Pointer` candidate |
| **§2.2** Immutable snapshots use `atomic.Pointer[T]` | N/A | N/A | ❌ Uses RWMutex where atomic pointer applies |
| **§3** Sharded locks for high-cardinality per-key state | N/A | N/A | ⚠️ Single global lock over the exporters map — no sharding |
| **§4.1** Clone pdata retained past `Consume*` | N/A (synchronous) | ✅ `CopyTo` before marshal | ✅ `CopyTo` per destination |
| **§4.2** Ownership-transfer swap for aggregates | N/A | N/A | N/A (no aggregation) |
| **§5.1** Client pool sized ≥ `num_consumers` | ✅ `grpc.ClientConn` multiplexes | ✅ franz-go handles internally | ✅ Delegates to per-backend OTLP |
| **§5.2** Retry request immutability | ✅ exporterhelper-owned | ✅ exporterhelper-owned | ✅ exporterhelper-owned per backend |
| **§5.6** Shutdown waits for in-flight | ✅ Delegated | ✅ Delegated + producer.Close | ✅ Explicit `consumeWG` per wrapped exporter |
| **§5** Documented race-free hot path | ✅ Lock-free | ✅ Lock-free (producer is thread-safe) | ❌ Known race in issue #1690 |

**Reading:** OTLP and Kafka are effectively at the SOP's ceiling — they get away with almost no synchronization because their state is either immutable or delegated to a library that handles concurrency itself. Load Balancing is where the SOP's advice starts to bite: it picks the third-best primitive for a case where the second-best (atomic pointer) applies cleanly.

---

## 3. Ranking table

Scoring rubric (5 = best): **Locking granularity** (finer = higher), **Throughput handling** (contention-free hot path = higher), **Race surface area** (fewer/smaller windows = higher). Rank is the **sum**; higher is better.

| Exporter | Locking granularity | Throughput handling | Race surface area | Total | Rank |
|---|---:|---:|---:|---:|---:|
| **OTLP** | 5 — no locks; whole state is immutable-after-Start | 5 — hot path is a struct-field read + gRPC call | 5 — no runtime-mutable shared state | **15** | 🥇 1 |
| **Kafka** | 5 — no mutexes; `sync.Pool` is lock-free amortized | 5 — franz-go producer is thread-safe; pooled buffers minimize allocations | 5 — pdata cloned before enqueue; producer owns the wire | **15** | 🥇 1 (tie) |
| **Load Balancing** | 2 — single `RWMutex` protects the ring + exporters map together (SOP §3 recommends sharding or atomic-pointer snapshots) | 3 — `RLock` on every unique trace ID; degrades past ~8 concurrent readers per [golang/go#17973](https://github.com/golang/go/issues/17973) | 2 — documented TOCTOU race in issue [#1690](https://github.com/open-telemetry/opentelemetry-collector-contrib/issues/1690); ring swap during in-flight export can drop data | **7** | 🥉 3 |

The tie at the top is real: OTLP and Kafka solve different problems, but both do so with the SOP's minimum-strength primitive — nothing at all in OTLP's case, and a lock-free allocator pool in Kafka's.

The gap to Load Balancing isn't a criticism of the code (dynamic backends are genuinely harder) — it's a demonstration of why the SOP prefers immutable-snapshot swaps: the RWMutex approach costs both throughput *and* correctness in this specific workload.

---

## 4. Applied lessons for your exporter

Cross-reference these against your implementation to decide whether it needs optimization:

**Match your workload to the winner:**

- **If your exporter's state is fully known at `Start`** (config, credentials, client, headers): follow OTLP. No locks. Struct-field reads on the hot path. `Start` happens-before `Consume*` gives you the memory barrier for free. Anything more is overhead.

- **If your exporter fans out or reformats data before shipping** (batching, marshaling, per-request buffers): follow Kafka. Use `sync.Pool` for buffer reuse (not for mutual exclusion). Delegate concurrency to a thread-safe transport client. Always `CopyTo` pdata that escapes `Consume*` when `MutatesData: false`.

- **If your exporter has runtime-mutable routing/config** (dynamic backends, hot-reload, per-tenant tables): do **not** copy Load Balancing directly. Prefer SOP §2.2 (`atomic.Pointer[snapshot]`) over `sync.RWMutex`. A snapshot bundling `ring + exporters` (and a version counter for TOCTOU checks in the export path) closes both the throughput cliff and much of issue #1690's race window.

**Specific red flags to check in your code:**

1. **Any `RLock` inside a per-span or per-datapoint loop.** That is the Load Balancing anti-pattern. Hoist the lookup outside the loop, or replace with `atomic.Pointer.Load()`.
2. **Any goroutine that retains a `ptrace.Traces` / `pmetric.Metrics` / `plog.Logs` past `Consume*` return without a `CopyTo`.** All three reference exporters do this correctly — Kafka copies before marshal, Load Balancing copies per destination. If you don't, you have a race whether or not you have locks.
3. **`sync.Mutex` guarding state that is only ever wholesale-replaced.** Convert to `atomic.Pointer[T]`. Every reference exporter avoids this: OTLP by never replacing, Kafka by using `sync.Pool` (which is lock-free), Load Balancing by (arguably wrongly) using an RWMutex.
4. **Missing shutdown wait.** OTLP relies on exporterhelper; Kafka calls `producer.Close(ctx)`; Load Balancing has an explicit `consumeWG` on each `wrappedExporter`. If your exporter has custom goroutines (batchers, retryers, resolvers) they must all be joined in `Shutdown`.
5. **Multiple locks with an implicit ordering.** The K8s resolver holds `updateLock` and `changeCallbackLock` in different orders in different methods — surviving only because the callers are disjoint. If you have two locks that ever appear in the same call graph, either merge them or document an acquisition order.

**Concrete pattern to lift from OTLP:** the "populate in Start, read everywhere else" pattern is the highest-throughput correctness-preserving design in the codebase. Push as much of your state as possible into it.

**Concrete pattern to lift from Kafka:** `sync.Pool` for per-export scratch buffers is essentially free — franz-go's throughput benefits significantly from not allocating `[]kgo.Record` on every export.

**Concrete pattern to avoid from Load Balancing:** the coarse `RWMutex` around `ring + exporters`. If your exporter has anything shaped like a routing table, prefer `atomic.Pointer[routingSnapshot]` per SOP §2.2, and use a version counter or reference-counted handle to close the in-flight-during-swap window.

---

## Source references

- [OTLP exporter — otlp.go](https://github.com/open-telemetry/opentelemetry-collector/blob/main/exporter/otlpexporter/otlp.go)
- [OTLP exporter — factory.go](https://github.com/open-telemetry/opentelemetry-collector/blob/main/exporter/otlpexporter/factory.go)
- [Kafka exporter — kafka_exporter.go](https://github.com/open-telemetry/opentelemetry-collector-contrib/blob/main/exporter/kafkaexporter/kafka_exporter.go)
- [Kafka exporter — factory.go](https://github.com/open-telemetry/opentelemetry-collector-contrib/blob/main/exporter/kafkaexporter/factory.go)
- [Load Balancing exporter — loadbalancer.go](https://github.com/open-telemetry/opentelemetry-collector-contrib/blob/main/exporter/loadbalancingexporter/loadbalancer.go)
- [Load Balancing exporter — trace_exporter.go](https://github.com/open-telemetry/opentelemetry-collector-contrib/blob/main/exporter/loadbalancingexporter/trace_exporter.go)
- [Load Balancing exporter — resolver_k8s.go](https://github.com/open-telemetry/opentelemetry-collector-contrib/blob/main/exporter/loadbalancingexporter/resolver_k8s.go)
- [Load Balancing race issue #1690](https://github.com/open-telemetry/opentelemetry-collector-contrib/issues/1690)
- [Go runtime — RWMutex scaling issue #17973](https://github.com/golang/go/issues/17973)
