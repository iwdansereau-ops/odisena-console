# Mutex Contention Hotspots — Ranked Report

**Component:** `aggregatorprocessor`
**Source profile:** `profile.mutex.pprof` (30 s capture, `AGGREGATOR_PROFILE=1`)
**Workload:** 100k working set · 50% rotation every 10 ms · 8 producers · 100 ms TTL · 20 ms eviction · 50 ms flush
**Max ingest latency observed during capture:** 272 ms
**Total contended wait time attributed to `stateMap.mu`:** **175.44 s** (aggregate goroutine-time over 30 s wall clock)

## Method

`runtime.SetMutexProfileFraction(1)` was enabled for the whole run so every contention event is recorded. Go's mutex profile attributes each event to the stack of the goroutine **holding** the lock at the moment another goroutine had to wait — this is the correct attribution for "which code paths cause other callers to wait?".

CPU profiling ran in parallel to verify the wait is off-CPU (blocked goroutine, no cycles consumed). The 175 s of mutex wait vs. 12.7 s of on-CPU `upsertNumeric` time confirms this is a lock-queueing problem, not a compute problem.

The lock observer (`LockObserver` in `state.go`) was deliberately **not** installed during the profiled run so pprof attribution is not distorted by observer overhead inside the critical section.

## Ranked hotspots

| Rank | Function | Wait time | % of total wait | Path |
|:---:|---|---:|---:|---|
| **1** | `stateMap.upsertNumeric` | **126,334 ms** | **72.01%** | `ConsumeMetrics → ingestMetric → ingestNumeric → upsertNumeric` |
| **2** | `stateMap.evictOlderThan` | **32,992 ms** | **18.80%** | `runEvictionLoop → evictOlderThan` |
| **3** | `stateMap.drain` | **15,795 ms** | **9.00%** | `runFlushLoop → flush → drain` |

All three flows are 100% non-overlapping — every call funnels into the single `stateMap.mu`. Rank 1 + 2 + 3 together account for **99.82%** of all mutex wait time in the process.

## Interpretation

### #1 — `upsertNumeric` (72% of wait time)

Every ingested data point takes the lock. At the observed ingest rate of ~500 k upserts/s across 8 producers, this is 4M lock acquisitions/second competing for one mutex. Most releases are fast (~120 µs p50 hot-path), but two things push the tail:

- **New-key inserts trigger map growth.** Go's built-in `map` rehashes on bucket expansion; a 50%-rotation workload adds 50 k new keys every 10 ms, so growth is frequent. The observed max critical section held is 11–14 ms, driven by growth events.
- **Eviction and drain contend on the same mutex.** When a large evict (5 ms held) lands mid-batch, all 8 producers queue behind it.

### #2 — `evictOlderThan` (19% of wait time)

Fires every 20 ms and scans the **entire** map, holding the write lock the whole time — even when 90%+ of entries are not stale. At 30–40 k live series the p99 held time is 2.4–4.6 ms; on a colder-cache tick it reaches 5–10 ms. Because it runs on its own goroutine, its wait attribution is small (the producers wait *for* it, not the other way around), but its impact on producer tail latency is disproportionate — each evict blocks all 8 producers simultaneously.

### #3 — `drain` (9% of wait time)

Fires every 50 ms during the flush loop. Walks and clears the entire map, so held time scales linearly with working-set size. Observed p99 is 4–7 ms, max ~10 ms. Similar to evict, drain is a single-writer path whose direct wait attribution is modest but whose impact on producer tail is significant.

## Recommendations, keyed to the ranked findings

### Immediate: shard the map (fixes all three ranks)

Replace the single `stateMap` with **N stripes** (32 or 64), each with its own `sync.Mutex` and its own `map[seriesKey]*aggregatedSeries`. Dispatch every operation by `key.hi % N`.

Effect per rank:

- **#1 (upsertNumeric):** each producer contends on 1 of N stripes chosen by its data point's hash. With 8 producers and 32 stripes, the expected simultaneous-collision rate drops by ~32×. Growth events also become per-stripe (each stripe holds ~1/N of the working set), so growth-triggered critical sections shrink to ~0.5 ms max.
- **#2 (evictOlderThan):** the eviction loop iterates stripes one at a time. Each stripe is acquired independently, held for ~1/N of the current held time (150–300 µs), and released before the next stripe is touched. Producers stall on at most one stripe at a time, and only 1/N of them at a time.
- **#3 (drain):** identical story to evict — flush drains stripes serially, each drain held time drops ~N×. The output batch is rebuilt from the concatenation of stripe drains.

Expected result: the observed 11–14 ms max critical section shrinks to ~0.5 ms, upsert-wait p99 (currently 16–27 ms) drops to well under 1 ms, and the 200 ms ConsumeMetrics budget stops being tight — it becomes 200×+ overspec.

### Follow-up: two-phase eviction (marginal after sharding)

Even after sharding, `evictOlderThan` still holds each stripe for a full O(stripe_size) scan. A two-phase pattern — acquire → collect stale keys into a local slice → release → reacquire → delete — halves the worst-case held time per stripe. Low priority once sharding is in.

### Not worth doing

- **`sync.RWMutex`:** every state-map operation is a writer; readers don't exist. No benefit.
- **Lock-free map (`sync.Map`):** wrong access pattern. `sync.Map` optimizes for read-mostly workloads with rare writes; this is write-mostly. Would regress.

## Reproducing the numbers

```bash
# Capture (30s of profiling):
AGGREGATOR_PROFILE=1 go test -run TestProfileChurnWorkload -timeout 3m -v ./...

# Top mutex functions:
go tool pprof -top -unit=ms profile.mutex.pprof

# Filtered to stateMap only:
go tool pprof -top -unit=ms \
  -focus="stateMap" \
  -show='\(\*stateMap\)\.(upsert|evict|drain|len)' \
  profile.mutex.pprof

# Interactive exploration (web UI):
go tool pprof -http=:0 profile.mutex.pprof
```

Artifacts written by the test: `profile.cpu.pprof`, `profile.mutex.pprof`, `profile.block.pprof`, plus `.txt` companions for the mutex and block profiles.
