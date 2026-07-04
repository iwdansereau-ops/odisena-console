## Staging memory check

<!-- gomem-staging-memory-check -->
### ⚠️ Allocation churn on `churn11`

**Allocation churn / GC thrash — not a leak.**

- **Verdict:** `ALLOC_CHURN`
- **Total `inuse_space` delta:** 0 B
- **HeapInuse delta (runtime.MemStats):** 48.0 KB
- **TotalAlloc delta:** 7.33 GB (625.12 MB/s sustained)
- **NumGC delta:** 1680 cycles (139.85 GC/s, avg pause 0.04 ms)
- **Snapshots:** 5 heap + 5 gcstats over 12s
- **Threshold per function:** 500.0 KB (`flat_delta`)
- **Deployed commit:** [`churn11`](../commit/churn111111111111111111111111111111111111)
- **Full report + SVG call graph:** [workflow run](https://github.com/example/repo/actions/runs/999) (download the `gomem-staging-churn11` artifact)

#### Why this verdict

- Allocation churn: 625.12 MB/s allocated, 139.85 GC/s, churn ratio 160201.8× (threshold 20×).
- NumGC Δ: 1680 cycles in 12s, avg pause 0.04ms, GC CPU fraction 2.0%.

#### GC & allocation metrics (first → last snapshot)

| Metric | Value |
|---|---|
| `TotalAlloc` Δ | 7.33 GB |
| Sustained alloc rate | 625.12 MB/s |
| `NumGC` Δ | 1680 cycles |
| GC frequency | 139.85 /s |
| Avg GC pause | 0.04 ms |
| GC CPU fraction (end) | 2.00% |
| `HeapInuse` Δ | 48.0 KB |
| `HeapObjects` Δ | +80 |
| Churn ratio (alloc/retained) | 160201.8× |

> ℹ️  **Interpretation:** the process is allocating aggressively but GC is reclaiming the bytes each cycle — `HeapInuse` stayed roughly flat while `TotalAlloc` and `NumGC` climbed. This is a **CPU / latency** regression (GC pause growth, wasted allocation on hot paths), not a memory leak. Look for temporary slice/map allocations inside tight loops that could be pooled with `sync.Pool` or hoisted out of the hot path.

#### Suggested next steps (allocation churn)

- Profile with `go tool pprof -alloc_objects` (or the artifact's profiles) to find hot allocation sites — these are usually more actionable than the `inuse_space` top-N for churn regressions.
- Look for per-request allocations that could reuse buffers via `sync.Pool` or pre-sized slices/maps.
- Check for `[]byte(str)` / `string([]byte)` conversions in hot loops, `fmt.Sprintf` for concatenation, and log statements that format even when the level is disabled.
- GC frequency of 139.85/s with 0.04 ms average pause suggests tuning `GOGC` or `GOMEMLIMIT` if the fix isn't obvious from the code.

Reproduce locally against the same commit:

```bash
git checkout churn111111111111111111111111111111111111
go build -o bin/gomem ./cmd/gomem
./scripts/staging-capture.sh $STAGING_PPROF_URL 180 5
./bin/gomem serve --dir ./profiles --reports ./reports
```

_This comment is updated in place by the `staging-memory-check` workflow after every successful staging deploy._
