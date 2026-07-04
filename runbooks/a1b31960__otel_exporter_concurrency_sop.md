# SOP: Thread-Safe, Atomic State Updates in Custom OpenTelemetry Collector Exporters

**Audience:** Go engineers building custom exporters against `go.opentelemetry.io/collector` v0.100+ (Collector API v1 / pdata stable).
**Scope:** Concurrency correctness for exporter-internal state (config snapshots, routing tables, credentials, in-flight counters, per-tenant buckets), safe handling of `pdata` payloads through `ConsumeTraces/Metrics/Logs`, and benchmarking locking overhead against the exporterhelper's queue/batcher senders.

---

## 1. Concurrency contract of a Collector exporter

Before choosing a primitive, internalize the invariants the Collector runtime already gives you:

- **`Consume{Traces,Metrics,Logs}` may be called concurrently by multiple goroutines.** The exporterhelper spawns `num_consumers` workers that drain the sending queue in parallel, and the pipeline may fan out from batching/routing processors. Any state that these calls touch is shared.
- **`pdata` payloads are conditionally immutable.** Per the [exporter README](https://github.com/open-telemetry/opentelemetry-collector/blob/main/exporter/README.md), an exporter that does **not** advertise `MutatesData: true` in its `Capabilities()` MUST NOT modify the `ptrace.Traces` / `pmetric.Metrics` / `plog.Logs` argument. The Collector shares that pointer across all non-mutating consumers; if you declare mutation, the fan-out layer clones for you.
- **`pdata` instances themselves are not internally synchronized.** The [pdata README](https://github.com/open-telemetry/opentelemetry-collector/blob/main/pdata/README.md) states each instance "cannot contain a reference to an object that is used in another pdata instance" — meaning safety comes from *not sharing writable references*, not from locks inside pdata. If you retain a `ptrace.Traces` past the return of `ConsumeTraces`, you must guarantee no other goroutine (including the caller that is about to reuse or free it) can touch it.
- **`Start` / `Shutdown` bracket everything.** State published during `Start` is safe to read lock-free from `Consume*` because `Start` happens-before any Consume call. State that changes at runtime (reloaded config, dynamic routing) needs a real primitive.

The primitive choice is a function of the read/write ratio and the granularity of the state:

| State shape | Read/write ratio | Recommended primitive |
|---|---|---|
| Single pointer to an immutable snapshot (config, routing table) | Read-heavy, occasional swap | `atomic.Pointer[T]` |
| Multi-field struct with invariants across fields | Mixed | `sync.RWMutex` guarding the struct |
| Independent per-key state (per-tenant, per-endpoint) | Any, but hot | Sharded `sync.RWMutex` |
| Single counter / gauge | Any | `sync/atomic` scalar (`atomic.Int64`, `atomic.Uint64`) |
| One-shot publication (init once, read forever) | Publish 1×, read ∞ | `sync.Once` + plain field |

---

## 2. `sync.RWMutex` vs `atomic.Value` / `atomic.Pointer[T]`

### 2.1 When RWMutex is the right answer

Use `sync.RWMutex` when the critical section must maintain invariants across **multiple fields** or performs a **read-modify-write** that a single atomic swap cannot express.

```go
type endpointState struct {
    mu           sync.RWMutex
    healthy      bool
    lastError    error
    consecFail   int
    nextRetryAt  time.Time
}

func (e *endpointState) recordFailure(err error, backoff time.Duration) {
    e.mu.Lock()
    defer e.mu.Unlock()
    e.healthy = false
    e.lastError = err
    e.consecFail++
    e.nextRetryAt = time.Now().Add(backoff)
}

func (e *endpointState) shouldSend() (bool, time.Time) {
    e.mu.RLock()
    defer e.mu.RUnlock()
    return e.healthy || time.Now().After(e.nextRetryAt), e.nextRetryAt
}
```

**Caveat — RWMutex scales poorly under contention.** The Go runtime issue [golang/go#17973](https://github.com/golang/go/issues/17973) documents that `RWMutex.RLock` uses an atomic increment on a single word, and the resulting cache-line ping-pong makes read throughput *decrease* past ~8–16 cores under high read rates. Rule of thumb: if your `RLock` sits on the hot path of every batch and you see >4 concurrent consumers, benchmark against an atomic pointer before shipping.

### 2.2 When to swap to `atomic.Pointer[T]`

If the state is (or can be made) **immutable after publication**, replace the RWMutex with `atomic.Pointer[T]` (Go 1.19+, preferred over `atomic.Value` because it is type-safe and avoids interface boxing).

```go
type routingSnapshot struct {
    // Immutable after Store: never mutate fields; publish a fresh *routingSnapshot instead.
    byTenant map[string]*endpoint
    default_ *endpoint
    version  uint64
}

type routingTable struct {
    snap atomic.Pointer[routingSnapshot]
}

// Hot path: zero locks, zero allocations.
func (r *routingTable) routeFor(tenant string) *endpoint {
    s := r.snap.Load()
    if ep, ok := s.byTenant[tenant]; ok {
        return ep
    }
    return s.default_
}

// Config reload path: build a fresh snapshot, then swap.
func (r *routingTable) reload(cfg *Config) {
    next := &routingSnapshot{
        byTenant: make(map[string]*endpoint, len(cfg.Tenants)),
        default_: buildEndpoint(cfg.Default),
        version:  atomic.AddUint64(&r.versionCtr, 1),
    }
    for k, v := range cfg.Tenants {
        next.byTenant[k] = buildEndpoint(v)
    }
    r.snap.Store(next) // release-store; readers see fully constructed snapshot.
}
```

**Rules that keep this correct:**

1. **Treat the pointed-to value as fully immutable after `Store`.** Never modify `snap.byTenant` in place — even adding a key on the writer side is a data race because a concurrent reader is doing an unsynchronized map lookup.
2. **Build the entire new snapshot before storing.** The atomic store is the release barrier; every field write must happen-before that store, from the perspective of a reader that does `Load`.
3. **Prefer `atomic.Pointer[T]` over `atomic.Value`.** `atomic.Value` requires all `Store` calls to use the *same concrete dynamic type* and panics otherwise; it also boxes into an `interface{}`, adding indirection. `atomic.Pointer[T]` is generic, type-checked at compile time, and compiles to the same underlying `LOCK CMPXCHG`/`MOV` on amd64/arm64 as a raw pointer atomic — see the [Go standard library discussion of atomic value patterns](https://blog.dubbelboer.com/2015/08/23/rwmutex-vs-atomicvalue-vs-unsafepointer.html) for historical benchmarks (unsafe.Pointer ≈ atomic.Value ≈ 10–20× faster on the read path than RWMutex under contention).
4. **Never take the address of a field inside the snapshot and hand it out with a longer lifetime than the snapshot itself.** Callers should either dereference and copy, or hold the whole snapshot pointer for the duration of use.

### 2.3 Decision heuristic

Ship `atomic.Pointer[T]` when **all four** hold:
- The state is naturally a whole-object replacement (config, cert bundle, routing table, header map).
- Writes are rare (config reload, cert rotation) relative to reads (every export).
- Readers only need a consistent view, not modification.
- No cross-field CAS is required.

Otherwise, ship `sync.RWMutex` (or a plain `sync.Mutex` if reads are not dominant — RWMutex only pays off when the read:write ratio exceeds ~10:1 and holds are short).

---

## 3. Sharded locking for high-throughput pipelines

When state is a map keyed by tenant, endpoint, or trace ID, a single mutex becomes the pipeline's bottleneck. The standard remedy is **striped/sharded locking**: partition the key space into N independent shards, each with its own lock. This is the same pattern `sync.Map` uses internally and what Java's `ConcurrentHashMap` popularized.

### 3.1 Reference implementation

```go
// shardCount MUST be a power of two so we can mask instead of mod.
const shardCount = 64
const shardMask = shardCount - 1

type shard struct {
    mu    sync.RWMutex
    state map[string]*tenantBucket
    _pad  [40]byte // pad to avoid false sharing across shards on a 64-byte line
}

type ShardedBuckets struct {
    shards [shardCount]shard
}

func New() *ShardedBuckets {
    s := &ShardedBuckets{}
    for i := range s.shards {
        s.shards[i].state = make(map[string]*tenantBucket)
    }
    return s
}

// fnv1a is inlined and branch-free; xxhash is a fine alternative.
func shardIdx(key string) uint64 {
    var h uint64 = 1469598103934665603
    for i := 0; i < len(key); i++ {
        h ^= uint64(key[i])
        h *= 1099511628211
    }
    return h & shardMask
}

func (s *ShardedBuckets) Get(key string) (*tenantBucket, bool) {
    sh := &s.shards[shardIdx(key)]
    sh.mu.RLock()
    b, ok := sh.state[key]
    sh.mu.RUnlock()
    return b, ok
}

func (s *ShardedBuckets) GetOrCreate(key string, factory func() *tenantBucket) *tenantBucket {
    sh := &s.shards[shardIdx(key)]
    // Fast path: read lock.
    sh.mu.RLock()
    b, ok := sh.state[key]
    sh.mu.RUnlock()
    if ok {
        return b
    }
    // Slow path: upgrade to write lock and re-check.
    sh.mu.Lock()
    defer sh.mu.Unlock()
    if b, ok = sh.state[key]; ok {
        return b
    }
    b = factory()
    sh.state[key] = b
    return b
}
```

### 3.2 Sizing and pitfalls

- **Pick shard count ≥ 4× `GOMAXPROCS`.** For 16 cores, 64 shards leaves ample headroom for skewed keys. Powers of two let you replace `%` (10–20 cycles) with `&` (1 cycle).
- **Pad each shard to a full cache line (64 bytes on x86-64, 128 bytes on some ARM64 configurations).** Without padding, two mutexes on adjacent shards share a cache line and every lock acquisition invalidates the neighbor's line — false sharing can silently regress a 32-shard map to worse than a single mutex.
- **Use a good hash.** `hash/maphash` (Go 1.19+) is the correct choice when keys are attacker-controlled; FNV-1a or xxhash is fine internally. Do **not** use `key[0]` or `len(key)` — real-world tenant IDs cluster and you will lose most shards to hot-spot skew.
- **Avoid cross-shard operations.** Any operation that must lock more than one shard reintroduces serialization and risks deadlock. If you need a global snapshot, acquire shards in a **fixed index order** and consider whether an `atomic.Pointer` to an immutable global snapshot would serve you better.
- **Watch shard-level hot keys.** Sharding fixes uniformly distributed load; a single "noisy neighbor" tenant that pins one shard is unaffected. For that case, add a second layer: per-key `atomic.Int64` counters inside the bucket so the hot key doesn't serialize on the shard's `Lock()`.

### 3.3 Sharded state in the export path — worked example

```go
type Exporter struct {
    buckets *ShardedBuckets       // per-tenant rate limiters
    routes  atomic.Pointer[routingSnapshot]
    inflight atomic.Int64          // for backpressure metrics
}

func (e *Exporter) ConsumeTraces(ctx context.Context, td ptrace.Traces) error {
    e.inflight.Add(1)
    defer e.inflight.Add(-1)

    // Read-only route lookup — no locks.
    snap := e.routes.Load()

    // Group spans by tenant WITHOUT mutating td (Capabilities().MutatesData == false).
    perTenant := map[string][]ptrace.ResourceSpans{}
    rss := td.ResourceSpans()
    for i := 0; i < rss.Len(); i++ {
        rs := rss.At(i)
        tenant, _ := rs.Resource().Attributes().Get("tenant.id")
        perTenant[tenant.Str()] = append(perTenant[tenant.Str()], rs)
    }

    // Per-tenant bucket lookup — sharded, mostly read-locked.
    var wg sync.WaitGroup
    errs := make(chan error, len(perTenant))
    for tenant, groups := range perTenant {
        bucket := e.buckets.GetOrCreate(tenant, func() *tenantBucket {
            return newBucket(snap.byTenant[tenant])
        })
        wg.Add(1)
        go func(t string, g []ptrace.ResourceSpans, b *tenantBucket) {
            defer wg.Done()
            errs <- b.send(ctx, cloneResourceSpans(g)) // clone since we escape ConsumeTraces
        }(tenant, groups, bucket)
    }
    wg.Wait()
    close(errs)
    return errors.Join(collect(errs)...)
}
```

Two properties worth flagging:

- **The route snapshot pointer is loaded exactly once per call.** Even if a concurrent config reload swaps in a new snapshot mid-call, this call keeps a consistent view. That is the entire point of the atomic-pointer pattern.
- **Data that escapes `ConsumeTraces` is cloned.** `ResourceSpans` is a handle into `td`; retaining it past the return of `ConsumeTraces` is a race against the pipeline's caller (which is free to mutate or pool the underlying arena).

---

## 4. Safely swapping immutable pdata-based snapshots

`pdata` objects are handles over an internal arena. There are three common scenarios where an exporter needs to keep pdata across goroutines, and each has a specific safe pattern.

### 4.1 Scenario A — Retain a payload past `ConsumeTraces` return

If your exporter buffers, aggregates, or asynchronously ships payloads, the incoming `ptrace.Traces` MUST be cloned (or the exporter must set `MutatesData: true` and treat the payload as its own).

```go
func (e *Exporter) Capabilities() consumer.Capabilities {
    return consumer.Capabilities{MutatesData: false} // we don't mutate, so we must clone if we retain.
}

func (e *Exporter) ConsumeTraces(ctx context.Context, td ptrace.Traces) error {
    // Deep copy: the pdata CopyTo methods perform a full clone into a fresh arena.
    owned := ptrace.NewTraces()
    td.CopyTo(owned)
    select {
    case e.buffer <- owned:
        return nil
    case <-ctx.Done():
        return ctx.Err()
    }
}
```

`CopyTo` is O(n) in the number of spans/points, so treat cloning as a real cost — but a correctness cost, not an optional one. See [pdata/ptrace](https://pkg.go.dev/go.opentelemetry.io/collector/pdata/ptrace) for the full API.

### 4.2 Scenario B — Publish a "current aggregate" snapshot for background flushing

Aggregating exporters (delta-to-cumulative, sampling reservoirs, per-attribute rollups) often maintain a running `pmetric.Metrics` that a flusher reads periodically. Do **not** share a live pdata object across the accumulator and the flusher — instead, atomic-swap ownership.

```go
type Aggregator struct {
    mu      sync.Mutex           // protects writes to `current`
    current pmetric.Metrics      // owned by whoever holds mu.Lock()

    // Flusher swaps `current` out atomically by locking briefly.
}

// Called from Consume path — many goroutines.
func (a *Aggregator) Add(md pmetric.Metrics) {
    a.mu.Lock()
    // Merge md into a.current. Safe: exclusive write access.
    mergeInto(a.current, md)
    a.mu.Unlock()
}

// Called from a single flusher goroutine.
func (a *Aggregator) Drain() pmetric.Metrics {
    fresh := pmetric.NewMetrics()
    a.mu.Lock()
    out := a.current      // transfer ownership
    a.current = fresh     // install empty replacement
    a.mu.Unlock()
    return out            // caller now uniquely owns `out`; no other goroutine holds a reference
}
```

The key insight: the mutex is held only long enough to **swap pointers**, not to serialize the export itself. `out` is uniquely owned after the swap, so the flusher can serialize/transmit it lock-free — including holding it across an HTTP round-trip — while new writes accumulate into `fresh`.

If you need lock-free reads of the aggregate (e.g. a metrics endpoint), replace the mutex with `atomic.Pointer[pmetric.Metrics]` and use CAS on the pointer, but then **all mutations must build a new `pmetric.Metrics` from scratch**, not modify the pointed-to instance.

### 4.3 Scenario C — Read-only fan-out to multiple exporters

If your custom exporter is one of several exporters in a pipeline, the Collector guarantees you receive the *same* pdata pointer as the others whenever no exporter mutates. This is documented in the [exporter README data-ownership section](https://github.com/open-telemetry/opentelemetry-collector/blob/main/exporter/README.md). Your exporter is therefore forbidden from mutating unless it declares `MutatesData: true`, in which case the fanout layer clones for you.

**Concrete rule:** if you find yourself calling any pdata setter (`SetName`, `Attributes().PutStr`, `Resource().Attributes().Remove`, etc.) on a value that arrived through `Consume*`, either:
- set `Capabilities().MutatesData = true`, accepting the clone cost the pipeline will impose upstream, or
- copy first with `CopyTo` and mutate the copy.

---

## 5. Avoiding race conditions during batch transmission

The exporterhelper's queue+batcher sender (see [`exporter/exporterhelper`](https://github.com/open-telemetry/opentelemetry-collector/blob/main/exporter/exporterhelper/README.md)) fans out to `num_consumers` goroutines and, with the newer batcher (issue [#8122](https://github.com/open-telemetry/opentelemetry-collector/issues/8122)), can also concurrently flush batches. That creates six race-prone spots:

**5.1 Sharing HTTP/gRPC clients — safe by design, but check timeouts.** `*http.Client` and gRPC `ClientConn` are safe for concurrent use. Do **not** wrap them in a mutex. Do configure `Transport.MaxConnsPerHost` and `MaxIdleConnsPerHost` to at least `num_consumers`, or the workers will serialize on a single TCP connection.

**5.2 Retries and the sending queue mutation.** A retried batch is the same `Request` submitted to the queue again. If your `Request.Export` retains references to internal exporter state, that state MUST NOT be mutated between the first attempt and the retry. Treat the `Request` as immutable once constructed. The [exporterhelper design](https://github.com/open-telemetry/opentelemetry-collector/issues/8122) mandates that the original request MUST NOT be mutated if `MergeSplit()` returns an error — the same principle applies to your `Export` implementation.

**5.3 In-flight counters.** Use `atomic.Int64.Add(1)` / `Add(-1)` in a `defer` around the export. Never guard a counter with a mutex you already hold for other reasons — you will accidentally serialize exports on the counter.

**5.4 Metrics and traces emitted by the exporter itself.** OpenTelemetry meters and tracers are safe for concurrent use. Instrument-level attribute sets, however, are not: build the attribute set once per request outside any hot loop.

**5.5 Ordered batch dispatch.** If your protocol requires per-key ordering (e.g. a per-trace-id backend), the exporterhelper's parallel workers will violate it. Two options:
- Use `num_consumers: 1` and rely on the batcher for throughput.
- Route by hash before the queue: run one exporter instance per shard, each with `num_consumers: 1`, and use a routing processor upstream. This is the pattern the OTel Arrow concurrent batch processor uses (see [contrib issue #33422](https://github.com/open-telemetry/opentelemetry-collector-contrib/issues/33422)).

**5.6 Shutdown races.** `Shutdown(ctx)` MUST wait for all in-flight exports to complete or the context to expire. Standard pattern:

```go
type Exporter struct {
    wg       sync.WaitGroup
    shutdown atomic.Bool
}

func (e *Exporter) ConsumeTraces(ctx context.Context, td ptrace.Traces) error {
    if e.shutdown.Load() {
        return errors.New("exporter is shutting down")
    }
    e.wg.Add(1)
    defer e.wg.Done()
    return e.send(ctx, td)
}

func (e *Exporter) Shutdown(ctx context.Context) error {
    e.shutdown.Store(true)
    done := make(chan struct{})
    go func() { e.wg.Wait(); close(done) }()
    select {
    case <-done:
        return nil
    case <-ctx.Done():
        return ctx.Err()
    }
}
```

Note the subtle ordering: `shutdown.Store(true)` happens-before `wg.Wait()` returns, but a Consume call that read `shutdown` as false may still be between the load and the `wg.Add(1)`. In practice this window is harmless because the queue has already been drained by the time `Shutdown` is called on the wrapped exporter — but if you build your own queue, close the queue's producer channel *before* setting the shutdown flag, and drain from `Shutdown`.

---

## 6. Benchmarking script — locking latency vs. throughput

Save the following as `internal/bench/lock_bench_test.go` inside your exporter module. It benchmarks four state-access strategies (single mutex, RWMutex, sharded RWMutex, atomic.Pointer snapshot) across four concurrency levels representative of production `num_consumers` settings.

```go
package bench

import (
    "hash/maphash"
    "runtime"
    "sync"
    "sync/atomic"
    "testing"
)

// -------- State under test --------

type routingEntry struct {
    endpoint string
    token    string
}

type snapshot struct {
    m map[string]*routingEntry
}

func makeSnapshot(n int) *snapshot {
    m := make(map[string]*routingEntry, n)
    for i := 0; i < n; i++ {
        k := keyFor(i)
        m[k] = &routingEntry{endpoint: "https://backend/" + k, token: "tok" + k}
    }
    return &snapshot{m: m}
}

func keyFor(i int) string {
    // 1024 hot keys — realistic tenant cardinality per collector.
    return string(rune('a'+i%26)) + string(rune('a'+(i/26)%26)) + string(rune('a'+(i/676)%26))
}

// -------- Strategy 1: single Mutex --------

type mutexStore struct {
    mu sync.Mutex
    s  *snapshot
}

func (x *mutexStore) get(k string) *routingEntry {
    x.mu.Lock()
    v := x.s.m[k]
    x.mu.Unlock()
    return v
}
func (x *mutexStore) swap(s *snapshot) { x.mu.Lock(); x.s = s; x.mu.Unlock() }

// -------- Strategy 2: RWMutex --------

type rwmutexStore struct {
    mu sync.RWMutex
    s  *snapshot
}

func (x *rwmutexStore) get(k string) *routingEntry {
    x.mu.RLock()
    v := x.s.m[k]
    x.mu.RUnlock()
    return v
}
func (x *rwmutexStore) swap(s *snapshot) { x.mu.Lock(); x.s = s; x.mu.Unlock() }

// -------- Strategy 3: sharded RWMutex --------

const shardN = 64
const shardMask = shardN - 1

type shardedStore struct {
    seed maphash.Seed
    shards [shardN]struct {
        mu sync.RWMutex
        m  map[string]*routingEntry
        _  [40]byte // pad to 64-byte line
    }
}

func newShardedStore(src *snapshot) *shardedStore {
    s := &shardedStore{seed: maphash.MakeSeed()}
    for i := range s.shards {
        s.shards[i].m = make(map[string]*routingEntry)
    }
    for k, v := range src.m {
        idx := s.idx(k)
        s.shards[idx].m[k] = v
    }
    return s
}

func (s *shardedStore) idx(k string) uint64 {
    var h maphash.Hash
    h.SetSeed(s.seed)
    h.WriteString(k)
    return h.Sum64() & shardMask
}

func (s *shardedStore) get(k string) *routingEntry {
    sh := &s.shards[s.idx(k)]
    sh.mu.RLock()
    v := sh.m[k]
    sh.mu.RUnlock()
    return v
}

// -------- Strategy 4: atomic.Pointer snapshot --------

type atomicStore struct {
    p atomic.Pointer[snapshot]
}

func (x *atomicStore) get(k string) *routingEntry { return x.p.Load().m[k] }
func (x *atomicStore) swap(s *snapshot)           { x.p.Store(s) }

// -------- Benchmarks --------

var sink *routingEntry

func benchStrategy(b *testing.B, get func(string) *routingEntry) {
    b.ReportAllocs()
    b.RunParallel(func(pb *testing.PB) {
        var local *routingEntry
        i := 0
        for pb.Next() {
            local = get(keyFor(i))
            i++
        }
        sink = local
    })
}

// Concurrency is controlled by GOMAXPROCS; use -cpu flag when invoking.
func BenchmarkMutex(b *testing.B) {
    x := &mutexStore{s: makeSnapshot(1024)}
    benchStrategy(b, x.get)
}

func BenchmarkRWMutex(b *testing.B) {
    x := &rwmutexStore{s: makeSnapshot(1024)}
    benchStrategy(b, x.get)
}

func BenchmarkSharded(b *testing.B) {
    x := newShardedStore(makeSnapshot(1024))
    benchStrategy(b, x.get)
}

func BenchmarkAtomicPointer(b *testing.B) {
    x := &atomicStore{}
    x.p.Store(makeSnapshot(1024))
    benchStrategy(b, x.get)
}

// -------- Read-heavy with concurrent writers (config reloads) --------

func BenchmarkAtomicPointer_WithWriter(b *testing.B) {
    x := &atomicStore{}
    x.p.Store(makeSnapshot(1024))
    stop := make(chan struct{})
    var wwg sync.WaitGroup
    wwg.Add(1)
    go func() {
        defer wwg.Done()
        for {
            select {
            case <-stop:
                return
            default:
                x.swap(makeSnapshot(1024)) // simulate config reload
                runtime.Gosched()
            }
        }
    }()
    benchStrategy(b, x.get)
    close(stop)
    wwg.Wait()
}

func BenchmarkRWMutex_WithWriter(b *testing.B) {
    x := &rwmutexStore{s: makeSnapshot(1024)}
    stop := make(chan struct{})
    var wwg sync.WaitGroup
    wwg.Add(1)
    go func() {
        defer wwg.Done()
        for {
            select {
            case <-stop:
                return
            default:
                x.swap(makeSnapshot(1024))
                runtime.Gosched()
            }
        }
    }()
    benchStrategy(b, x.get)
    close(stop)
    wwg.Wait()
}
```

### 6.1 How to run

Execute across representative core counts (matches typical `num_consumers`):

```bash
# One-off run comparing all four strategies at 1, 4, 16, 64 concurrent goroutines.
go test -bench=. -benchmem -benchtime=3s -cpu=1,4,16,64 ./internal/bench/... \
    | tee bench.out

# Statistical comparison across runs (install: go install golang.org/x/perf/cmd/benchstat@latest)
for i in 1 2 3 4 5; do
    go test -bench=. -benchtime=3s -cpu=16 ./internal/bench/... >> bench.raw
done
benchstat bench.raw
```

### 6.2 What to look for in the results

- **`BenchmarkMutex-64` degrades roughly linearly with GOMAXPROCS.** Every reader serializes on the same lock word.
- **`BenchmarkRWMutex-64` typically peaks around 4–8 goroutines then plateaus or regresses**, consistent with [golang/go#17973](https://github.com/golang/go/issues/17973). If you see `RWMutex` slower than plain `Mutex` at high concurrency, you've reproduced the runtime issue — the fix is either sharding or atomic pointer.
- **`BenchmarkSharded-64` should scale near-linearly** up to shardN and then plateau at the shard count. If it doesn't scale, check for false sharing (remove the `_ [40]byte` padding and observe the regression to confirm).
- **`BenchmarkAtomicPointer-64` should be the fastest read path** and effectively constant with concurrency, at the cost of a full map rebuild on every write. `BenchmarkAtomicPointer_WithWriter` versus `BenchmarkRWMutex_WithWriter` isolates the writer-starves-readers behavior of `RWMutex`.

### 6.3 End-to-end throughput harness (optional)

Micro-benchmarks measure the primitive in isolation. To measure the primitive's impact on **collector throughput**, wire the strategy into your exporter, disable network I/O (dial `127.0.0.1:0` and drop bytes), and drive it with the load generator:

```bash
git clone https://github.com/open-telemetry/opentelemetry-collector-contrib
cd opentelemetry-collector-contrib/testbed
# Adapt tests/trace_test.go to point at your local exporter build.
go test -run TestTraceNoBackend10kSPS -v ./tests/...
```

The testbed emits `metric_cpu_seconds`, `metric_ram_mib_max`, and `sent_spans` so you can attribute regressions to a specific primitive change.

---

## 7. Checklist before shipping a custom exporter

- [ ] `Capabilities().MutatesData` accurately reflects whether the exporter mutates the incoming pdata.
- [ ] Any pdata retained past `Consume*` return is `CopyTo`-cloned into an exporter-owned instance.
- [ ] Exporter-internal state chooses the minimum-strength primitive: atomic scalar → atomic.Pointer → RWMutex → sharded RWMutex, in that preference order for read-heavy state.
- [ ] `atomic.Pointer` snapshots are treated as immutable after `Store`; writers always build a new snapshot.
- [ ] Sharded maps use a power-of-two shard count, a well-distributed hash (`hash/maphash`), and per-shard padding.
- [ ] HTTP/gRPC clients have connection pool sizes ≥ `num_consumers`.
- [ ] `Shutdown` waits on in-flight exports with a WaitGroup or equivalent, honoring the passed `context.Context`.
- [ ] `go test -race ./...` passes with the exporter under load (drive it from the collector testbed or a custom stress harness).
- [ ] Micro-benchmarks recorded (§6) as a baseline; re-run before merging concurrency changes.

---

## References

- [OpenTelemetry Collector — Exporter README (data ownership)](https://github.com/open-telemetry/opentelemetry-collector/blob/main/exporter/README.md)
- [OpenTelemetry Collector — pdata README (mutable sharing rules)](https://github.com/open-telemetry/opentelemetry-collector/blob/main/pdata/README.md)
- [OpenTelemetry Collector — exporterhelper README (queue, batcher, retry)](https://github.com/open-telemetry/opentelemetry-collector/blob/main/exporter/exporterhelper/README.md)
- [Issue #8122 — Move batching to exporterhelper](https://github.com/open-telemetry/opentelemetry-collector/issues/8122)
- [Issue #11308 — Batch processor concurrency and error transmission](https://github.com/open-telemetry/opentelemetry-collector/issues/11308)
- [Contrib issue #33422 — Concurrent Batch Processor](https://github.com/open-telemetry/opentelemetry-collector-contrib/issues/33422)
- [ptrace package reference](https://pkg.go.dev/go.opentelemetry.io/collector/pdata/ptrace)
- [golang/go#17973 — sync: RWMutex scales poorly with CPU count](https://github.com/golang/go/issues/17973)
- [Erik Dubbelboer — RWMutex vs atomic.Value vs unsafe.Pointer](https://blog.dubbelboer.com/2015/08/23/rwmutex-vs-atomicvalue-vs-unsafepointer.html)
- [Texlution — Golang lock-free values with atomic.Value](https://texlution.com/post/golang-lock-free-values-with-atomic-value/)
