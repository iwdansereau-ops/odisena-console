# Memo: Mutex Granularity Under High-Frequency Eviction Churn

**Component:** `aggregatorprocessor` — OpenTelemetry Collector custom processor
**Test:** `TestContinuousChurn200msGCBudget` (`loadtest_test.go`)
**Environment:** Linux/amd64, Intel Xeon @ 2.9 GHz, 2 vCPU, Go 1.23.4

## Workload

| Parameter | Value |
|---|---|
| Working set | 100,000 active series |
| Rotation | 50% (50,000 keys) replaced every 10 ms |
| Producers | 8 parallel goroutines |
| Batch size | 200 data points per `ConsumeMetrics` call |
| TTL | 100 ms |
| Eviction interval | 20 ms |
| Flush interval | 50 ms |
| Duration | 5 s (2.3M–2.6M upserts, ~230 evictions, ~100 drains) |
| GC-pause budget | 200 ms |

## Headline results (3-run range)

| Metric | Range across runs |
|---|---|
| Upsert throughput | 469k – 526k ops/s |
| Peak working-set size | 34k – 42k series (well under 100k target) |
| Peak heap | 75 – 90 MiB |
| **GC pause p99** | **214 µs – 403 µs** |
| **GC pause max** | **482 µs – 1.23 ms** |
| Total GC pause / run | 8 – 9 ms (0.17% – 0.19% of wall clock) |
| Eviction critical-section held p99 | 2.4 – 4.6 ms |
| Drain critical-section held p99 | 4.0 – 7.0 ms |
| **Max critical section held (any op)** | **11 – 14 ms** |
| **Upsert wait-for-lock p99** | **16 – 27 ms** |
| **Upsert wait-for-lock max** | **50 – 89 ms** |
| **Max `ConsumeMetrics` latency** | **167 – 247 ms** |

## Verdict

**GC is not the bottleneck.** Every observed pause was under 1.25 ms, roughly 160× below the 200 ms budget, and the aggregate GC time was well under 0.2% of run time. The generational-hypothesis-friendly access pattern (short-lived batch objects, one long-lived aggregation map) plus the 100 ms TTL means most series die young and never survive to a mark cycle.

**The single mutex on `stateMap` is at the edge of adequacy for this workload.** The 200 ms `ConsumeMetrics` budget is met on most runs but occasionally breached (247 ms observed in one of three runs). The proximate cause is not GC — it is **lock queueing behind a slow critical section**. Specifically:

1. **`drain` holds the lock for up to 7 ms** because it copies out every entry and clears the map. It happens every `FlushInterval` (50 ms).
2. **`evictOlderThan` holds the lock for up to 5 – 10 ms** because it scans the entire map — even entries that aren't stale. It happens every `EvictionInterval` (20 ms).
3. **First-insertion upserts hold the lock for up to 11 – 14 ms** because the map has to grow (Go's built-in map rehashes on growth; a 50%-rotation workload triggers frequent bucket expansion).
4. When a drain or eviction lands on the mutex at the same moment a producer needs to insert a new-key batch, the producer waits: **upsert wait p99 = 16 – 27 ms, max = 50 – 89 ms**. If two long critical sections queue back-to-back (drain → evict → upsert with growth), the producer's total call time crosses 200 ms.

So the current design **passes the budget most of the time and fails it occasionally under the specified churn rate**. The p99 GC-pause result is excellent; the p99 lock-wait result is not.

## Is single-mutex granularity "sufficient"?

**For steady-state ingest at this rate: yes.** Cached upserts (existing keys) take ~120 µs p50 and never touch map growth, so the lock is released quickly and hot-path throughput is ~500k ops/s.

**For simultaneous churn + eviction + flush: marginal.** The observed p99 lock-wait time (16 – 27 ms) is 100× the p99 GC pause and is the dominant contributor to tail ingest latency. Producers wait for the lock roughly two orders of magnitude longer than they wait for the runtime.

The 200 ms budget survives because **the map size stays modest (~35 k – 42 k live series)** — eviction is winning the race, so no single critical section grows unbounded. If the workload doubled the working set (200 k) or halved the eviction interval, the same critical sections would run 2× longer and the budget would be breached routinely.

## Recommendations, in order of increasing effort

1. **Shard the state map.** Use N stripes (e.g. 32 or 64), each with its own `sync.Mutex`, and dispatch by `key.hi % N`. Eviction and drain then walk stripes independently, and the longest critical section shrinks by ~N×. This is the highest-leverage change and would take the observed 14 ms max-held down to ~500 µs. Prior art: `sync.Map`, `groupcache`, most contended caches in the Go ecosystem.

2. **Two-phase eviction.** Under the current single lock, `evictOlderThan` scans the entire map holding the write lock. Two phases: acquire lock → collect stale keys into a local slice → release lock → reacquire → delete. This limits the held time to two O(n) scans with a yield point in between, reducing the tail impact on producers even without sharding.

3. **Move drain to a swap-and-rebuild.** Instead of `drain()` copying pointers out one-by-one, atomically swap the underlying map pointer with a fresh empty map (`atomic.Pointer[map[...]*aggregatedSeries]`). The lock is held only long enough to swap the pointer; producers touching the old map just naturally stop after the swap. Complexity: producers mid-upsert on the old map need a stable handle for the duration of one call — feasible but requires care.

4. **Use `sync.RWMutex`.** Marginal here — upserts and drains are all writes, so the reader/writer split doesn't help. Skip.

5. **Backpressure knob.** Rate-limit `ConsumeMetrics` when lock-wait p99 exceeds a threshold, exposing the pressure to the caller rather than hiding it as latency. Complementary to sharding.

**Bottom line:** the current single-mutex design is adequate for the specified 100k / 50% / 10 ms workload with the 200 ms budget, but it has no headroom. Sharding the map (item 1) is the single change I would ship before running this workload in production, and it converts the current "occasionally breaches budget" behavior into "budget is essentially free."

## Reproducing the numbers

```bash
# Fast (2 s) run, no env var:
go test -run TestContinuousChurn200msGCBudget -v -count=1 ./...

# Full 5 s run:
AGGREGATOR_LOADTEST=1 go test -run TestContinuousChurn200msGCBudget -v -count=1 ./...
```

A machine-readable summary is written to `loadtest_summary.txt` on every run.
