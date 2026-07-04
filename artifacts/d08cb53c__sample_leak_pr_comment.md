<!-- gomem-staging-memory-check -->
### 🚨 Retention leak on `leak111`

**True retention leak detected.**

- **Verdict:** `RETENTION_LEAK`
- **Total `inuse_space` delta:** 7.31 MB
- **HeapInuse delta (runtime.MemStats):** 5.77 MB
- **TotalAlloc delta:** 10.62 MB (904.6 KB/s sustained)
- **NumGC delta:** 4 cycles (0.33 GC/s, avg pause 0.06 ms)
- **Snapshots:** 5 heap + 5 gcstats over 12s
- **Threshold per function:** 500.0 KB (`flat_delta`)
- **Deployed commit:** [`leak111`](../commit/leak1111111111111111111111111111111111111)
- **Full report + SVG call graph:** [workflow run](https://github.com/acme/repo/actions/runs/1) (download the `gomem-staging-leak111` artifact)

#### Why this verdict

- Per-function retention: `main.processBatch` retained 4.79 MB (> 500.0 KB threshold).
- HeapInuse Δ over the window: 5.77 MB.
- Allocation-to-retention ratio 1.8× is within normal range — this looks like retention, not GC thrash.

#### GC & allocation metrics (first → last snapshot)

| Metric | Value |
|---|---|
| `TotalAlloc` Δ | 10.62 MB |
| Sustained alloc rate | 904.6 KB/s |
| `NumGC` Δ | 4 cycles |
| GC frequency | 0.33 /s |
| Avg GC pause | 0.06 ms |
| GC CPU fraction (end) | 0.00% |
| `HeapInuse` Δ | 5.77 MB |
| `HeapObjects` Δ | +482 |
| Churn ratio (alloc/retained) | 1.8× |

> ℹ️  **Interpretation:** allocation-to-retention ratio is normal — bytes allocated are staying live. This looks like a real leak, not GC thrash.

#### Top 5 functions by retained bytes

| # | Function | Flat Δ | Cum Δ | Source |
|--:|----------|-------:|------:|--------|
| 1 🚨 | `main.processBatch` | 4.79 MB | 4.79 MB | `.../cmd/sample-processor/main.go:43` |
| 2 🚨 | `main.enqueueEvents` | 2.52 MB | 2.52 MB | `.../cmd/sample-processor/main.go:55` |

#### Suggested next steps (retention)

- Inspect `.../cmd/sample-processor/main.go:43`: unbounded slice/map appends · missing cache eviction · goroutine blocked on channel · `sync.Pool` retention · unclosed body / rows / file handle.
- Inspect `.../cmd/sample-processor/main.go:55`: unbounded slice/map appends · missing cache eviction · goroutine blocked on channel · `sync.Pool` retention · unclosed body / rows / file handle.

Reproduce locally against the same commit:

```bash
git checkout leak1111111111111111111111111111111111111
go build -o bin/gomem ./cmd/gomem
./scripts/staging-capture.sh $STAGING_PPROF_URL 180 5
./bin/gomem serve --dir ./profiles --reports ./reports
```

_This comment is updated in place by the `staging-memory-check` workflow after every successful staging deploy._
