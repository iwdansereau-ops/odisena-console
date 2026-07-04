# 2 TB RDS Stress-Test Routine

Companion to the [Zero-Downtime DDL Playbook](./zero_downtime_ddl_playbook.md) and the [2 TB Backfill Runbook](../../rds_2tb_backfill_runbook.md). This is the harness that **validates** the runbook — it drives realistic peak load against a staging pub+sub topology, injects a 50 % throughput spike, and proves that the documented pause/resume triggers actually fire when replay lag crosses 500 MB.

## What this harness answers

| Question | Answer produced |
|---|---|
| Can staging sustain production peak write load with ≤ 500 MB replay lag? | Phase 1 p95 band |
| What happens when a 50 % write-throughput spike hits mid-migration? | Phase 2 p95 + peak bands |
| How fast does the subscriber catch up after a spike? | Phase 3 catchup band |
| **Do the runbook's automated pause triggers actually work?** | Phase 4 pause-trigger verification |

If any answer is RED, the routine exits 1 and blocks the "ready for prod backfill" gate.

## Staging topology

```
┌───────────────────────────┐        logical replication         ┌───────────────────────────┐
│   Publisher (staging)     │ ──────── stress_pub ─────────────▶ │   Subscriber (staging)    │
│   db.r6g.4xlarge          │                                    │   db.r6g.4xlarge          │
│   custom param group      │                                    │   custom param group      │
│   local NVMe (fast)       │                                    │   local NVMe              │
│   ~15 GB seeded data      │                                    │                           │
└───────────────────────────┘                                    └───────────────────────────┘
        │                                                                    │
        └───────────────── monitor.sh samples both sides ─────────────────────┘
                              (1 s cadence → monitor.jsonl)
```

Not a full 2 TB clone — that's expensive and unnecessary. We seed **60 M rows across 4 tables (~22 GB)** which is enough to make the **replication path** hit the same code paths and produce the same WAL profile as 2 TB. What we don't reproduce: 2 TB of vacuum overhead. That's fine — vacuum is measured separately in prod monitoring.

## Five-phase experiment

| Phase | Duration | Load | Purpose |
|---|---|---|---|
| **0. Baseline** | 10 min | none | Establish idle replay-lag floor |
| **1. Steady + backfill** | 30 min | steady pgbench (20 clients, ~500 tps write) + concurrent chunked backfill | Simulate normal-day migration |
| **2. 50 % spike overlay** | 10 min | steady load **plus** spike overlay adding +50 % write throughput | The actual stress event |
| **3. Cool-down** | 10 min | no load | Measure time-to-catchup |
| **4. Failure-mode injection** | ~15 min | new backfill + synthetic lag/slot/CPU pressure | Prove pause triggers fire |

Phase timing is emitted to `phases.jsonl` and used by the analyzer to slice the monitor stream.

## Workload profiles

### `workloads/steady.sql` — 70/30 write/read mix

Approximates production OLTP: 40 % hot-row updates, 20 % event inserts, 10 % status transitions, 30 % point-lookup reads. Every txn stays under ~20 ms on baseline hardware.

### `workloads/spike.sql` — synthetic 50 % throughput spike

Runs in a **separate pgbench process**, all writes, with big-fanout INSERTs (20 rows per txn to `events` + `line_items`). Math: if steady drives N txn/s at 70 % writes, spike adds 0.5·N pure-write txn/s, taking net write rate from 0.7 N to 1.2 N — a **+71 % write increase**, calibrated to reproduce the "batch job + flash sale" profile that has historically caused real replay-lag incidents.

### `workloads/seed.sh` — populates the schema

```bash
PUB_URI=... ACCOUNTS_N=5000000 EVENTS_N=50000000 \
  bash ci/stress_test/workloads/seed.sh
```

Takes ~20 min on staging-class hardware. Idempotent — re-running only inserts if row counts are short.

### `workloads/backfill_loop.sql` — canonical chunked backfill

Exact pattern from the runbook (keyset pagination, `FOR UPDATE SKIP LOCKED`, per-chunk `lock_timeout`, external abort switch on `ops.backfill_progress.status`). Backfills `stress.users_bf.email_verified_at`.

## Success thresholds

The analyzer applies these bands. Any RED band fails the run.

### Phase 0 — baseline (no load)

| p95 replay_lag | Band |
|---|---|
| ≤ 10 MB | 🟢 GREEN |
| ≤ 50 MB | 🟡 YELLOW |
| > 50 MB | 🔴 RED |

Rationale: an idle system should have essentially zero replay lag. If baseline is > 50 MB, the subscriber is already unhealthy and the rest of the run is not interpretable.

### Phase 1 — steady load + backfill

| p95 replay_lag | Band |
|---|---|
| ≤ 200 MB | 🟢 GREEN |
| ≤ 500 MB | 🟡 YELLOW |
| > 500 MB | 🔴 RED |

Rationale: a normal-day backfill should not cross the runbook's **500 MB pause-trigger yellow line**. If it does, the chunk size or sleep interval is too aggressive and needs tuning before the real prod backfill.

### Phase 2 — 50 % spike overlay

| Metric | 🟢 GREEN | 🟡 YELLOW | 🔴 RED |
|---|---|---|---|
| p95 replay_lag | ≤ 500 MB | ≤ 1024 MB | > 1024 MB |
| peak replay_lag | ≤ 1024 MB | ≤ 1500 MB | > 1500 MB |

Rationale: peak momentarily crossing 1 GB during a legitimate spike is acceptable **provided p95 stays under 1 GB** — meaning the average subscriber can keep up and only brief transients breach the SLO. A peak above 1.5 GB indicates the subscriber cannot recover between spikes.

### Phase 3 — cool-down (time-to-catchup)

Measured from spike end to first replay_lag sample under 10 MB.

| Catchup duration | Band |
|---|---|
| ≤ 180 s | 🟢 GREEN |
| ≤ 600 s | 🟡 YELLOW |
| > 600 s | 🔴 RED |

Rationale: if the subscriber takes more than 10 minutes to catch up on a 10-minute spike, its apply throughput is fundamentally slower than the publisher's write rate — production will not recover on its own.

### Phase 4 — pause-trigger verification

The load-bearing test. Every synthesized breach must produce a matching pause.

| Signal | Verdict |
|---|---|
| 0 `pause_missing` events **and** p95 response latency ≤ `PAUSE_RESPONSE_S` (default 90 s) | 🟢 GREEN |
| observed pauses exist but with elevated latency | 🟡 YELLOW |
| **Any `pause_missing` event** | 🔴 RED — runbook's documented automation does not work |

## Failure-mode injection tests

Phase 4 runs one or more injection modes (comma-separated in `FAILURE_MODES`):

### `FAILURE_MODES=lag` — synthesize replay lag

Disables the subscription on the subscriber for 180 s while a backfill and steady load are running. WAL accumulates on the publisher, `replay_lag_bytes` climbs past 500 MB. The abort enforcer records `breach_start` → `pause_expected` → (waits) → `pause_observed` or `pause_missing`.

**Passes when:** `ops.backfill_progress.status` flips to `paused` within `PAUSE_RESPONSE_S` seconds of the breach holding for `TRIGGER_HOLD_S` seconds.

### `FAILURE_MODES=slot` — synthesize retained-WAL bloat

Disables the subscription for 5 minutes to force WAL retention on the publisher's replication slot. Validates that `retained_wal_bytes` monitoring works and that the slot recovers cleanly when re-enabled.

**Passes when:** slot returns to `active=true` and `retained_wal_bytes` decreases monotonically after re-enable.

### `FAILURE_MODES=cpu` — synthesize CPU pressure

Runs a 3× spike overlay for 5 minutes. Validates that publisher CPU pressure is correctly attributed to workload (not replication) in the metrics stream.

**Passes when:** `pub_commit_total` continues to grow (i.e. writes still land) and lag recovers post-spike within Phase 3 bounds.

## Failure-mode analysis — what to look at when lag > 500 MB

The runbook's abort criteria: replay_lag > 1 GB, retained_wal > 32 GB, chunk-timeout > 3/5min, CPU > 85 % for 5 min, free storage < 300 GB. When Phase 2 or Phase 4 records lag > 500 MB, work through this decision tree:

1. **Was the pause-trigger `pause_observed`?**
   - **Yes** → runbook works. Investigate why lag climbed (chunk size too big? spike bigger than modeled?). Tune backfill parameters and re-run.
   - **No** → the automation is broken. Do NOT proceed to prod. Check: (a) does the monitor/watchdog have DB access? (b) is the UPDATE on `backfill_progress` running? (c) are timezone/timestamp assumptions correct? Fix and re-run Phase 4.

2. **Was the `retained_wal_bytes` climb correlated with lag climb?**
   - **Yes** → the subscriber is the bottleneck (apply-side saturation). Look at subscriber CPU, IOPS, `pg_stat_subscription_stats`. Consider `synchronous_commit=off` on subscriber for logical apply worker (if consistency permits).
   - **No, WAL climbed without lag** → the slot is retaining WAL for another reason (e.g. inactive subscription elsewhere). List all logical slots.

3. **Did any chunk hit `lock_timeout`?**
   - **Yes, > 3 times in 5 min** → hot-row contention. Look at `pg_locks` samples in the artifacts; reduce chunk size and/or increase sleep_ms.
   - **No** → contention is not the issue.

4. **What does the phase 3 catchup look like?**
   - **> 600 s** → subscriber cannot keep up with publisher's steady write rate, independent of the spike. Consider: subscriber instance class upgrade, subscriber-side index review, or partition the publication.
   - **< 180 s** → the SLO breach was transient and the system self-heals; tune abort ceilings up if this is normal for your workload.

5. **Was there a `pause_missing` event?**
   - This is the loudest signal in the whole harness. It means: at the moment when the runbook is supposed to save you, it didn't. Root-cause and re-verify before any production backfill.

## Running it

```bash
# One-time: seed the schema (~20 min)
psql "$PUB_URI"  -f ci/stress_test/workloads/schema.sql
psql "$SUB_URI"  -f ci/stress_test/workloads/schema.sql   # subscriber needs tables too
PUB_URI=... bash  ci/stress_test/workloads/seed.sh
# ... configure subscription on subscriber pointing at stress_pub ...

# Run the full 5-phase experiment (~85 min end to end)
PUB_URI=...  SUB_URI=...  SUB_NAME=stress_sub \
  bash ci/stress_test/stress_test.sh

# Or run only a subset (e.g. skip baseline + spike, just do failure-mode)
PHASES=4  FAILURE_MODES=lag,slot \
  PUB_URI=... SUB_URI=... SUB_NAME=stress_sub \
  bash ci/stress_test/stress_test.sh

# Analyze
python3 ci/stress_test/lib/analyze.py /tmp/stress_test_<timestamp>
cat /tmp/stress_test_<timestamp>/report.md
```

## Artifacts produced

| File | Contents |
|---|---|
| `run.log` | Human-readable timeline |
| `phases.jsonl` | Phase start/end timestamps |
| `monitor.jsonl` | 1-second replication + backfill samples |
| `abort_events.jsonl` | Pause-trigger events (breach_start, pause_expected, pause_observed, pause_missing) |
| `pgbench_*.log` | Per-workload pgbench progress and TPS |
| `backfill*.log` | Backfill loop output |
| `report.md`, `report.json` | Analyzer verdict |

## Recommended cadence

- **Every quarter** — full 5-phase run against staging to catch drift in subscriber apply throughput.
- **Before any backfill > 10 M rows** — full 5-phase run with the actual backfill query.
- **After any change** to `wal_sender_timeout`, `max_slot_wal_keep_size`, or backfill chunk parameters — Phase 4 only, to re-verify pause triggers.
- **After incident** where lag exceeded 500 MB in prod — Phase 2 + Phase 4 to reproduce and prove the fix.

## Sources

- [PostgreSQL 16 — pgbench](https://www.postgresql.org/docs/16/pgbench.html)
- [PostgreSQL 16 — Monitoring Logical Replication](https://www.postgresql.org/docs/16/logical-replication-monitoring.html)
- [PostgreSQL 16 — pg_stat_replication](https://www.postgresql.org/docs/16/monitoring-stats.html#MONITORING-PG-STAT-REPLICATION-VIEW)
- [PostgreSQL 16 — pg_replication_slots](https://www.postgresql.org/docs/16/view-pg-replication-slots.html)
- [AWS RDS User Guide — PostgreSQL logical replication](https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/CHAP_PostgreSQL.html#PostgreSQL.Concepts.General.FeatureSupport.LogicalReplication)
