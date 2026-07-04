# PostgreSQL RDS Migration Tuning Runbook

**Scope:** Large-scale data migrations into Amazon RDS for PostgreSQL (single-node or Multi-AZ). Focused on maximizing sustained write throughput during bulk load while preventing WAL bottlenecks, checkpoint I/O storms, and CPU thrashing, then returning the instance to a steady-state OLTP configuration.

**Audience:** DBAs, SREs, and platform engineers executing a one-shot cutover or staged backfill.

---

## 1. Executive summary

Bulk loads into PostgreSQL misbehave for one dominant reason: **checkpoints fire too often**, and each checkpoint after a page's first modification forces a *full-page image* into WAL, amplifying write volume dramatically ([Cybertec](https://www.cybertec-postgresql.com/en/checkpoint-distance-and-amount-of-wal/), [Percona](https://www.percona.com/blog/importance-of-tuning-checkpoint-in-postgresql/)). Spreading checkpoints via larger `max_wal_size` and longer `checkpoint_timeout` can cut WAL generation by ~6x on write-heavy workloads and yields roughly a 10% baseline performance gain even before other tuning ([Percona](https://www.percona.com/blog/importance-of-tuning-checkpoint-in-postgresql/)).

The runbook targets four parameter families:

1. `checkpoint_timeout` + `checkpoint_completion_target`
2. `max_wal_size` (and `min_wal_size`)
3. `maintenance_work_mem` (for index builds, VACUUM, ANALYZE)
4. Autovacuum cost + threshold parameters

All values below are **starting points**. Validate against your instance class and observed Performance Insights load before locking them in.

---

## 2. Pre-flight checklist: memory allocation

Run every item before touching parameter groups. RDS parameters marked "static" (`shared_buffers`, `max_connections`, `max_wal_senders`, `wal_level`) require a reboot — plan for it.

### 2.1 Right-size the instance

| Check | Target | Notes |
|---|---|---|
| Source DB total size | Known within ±5% | Drives WAL sizing and storage headroom. |
| Peak row-write rate | Rows/sec measured on source | Sets required IOPS floor. |
| Instance class RAM | ≥ 2× active working set of hottest tables | Bulk loads still need cache for lookup/FK validation. |
| gp3/io2 IOPS | ≥ 3× steady-state peak, provisioned for load window | WAL flush is latency-sensitive. |
| Storage headroom | 3× projected WAL peak + 25% free at cutover | `max_wal_size` is a *soft* limit ([PostgreSQL docs](https://www.postgresql.org/docs/current/wal-configuration.html)). |
| Multi-AZ | Confirm sync replica lag budget | Sync replication amplifies commit latency; consider disabling during load only if RPO permits. |

### 2.2 Memory budget (per instance)

Compute before setting anything else. RDS default parameter group derives `shared_buffers` from `{DBInstanceClassMemory/32768}` pages — override this for migration workloads.

```
Total RAM = R
├── OS + RDS agents        ~1 GB (fixed)
├── shared_buffers         25% of R      (cap ~40% on very large instances)
├── effective_cache_size   50–70% of R   (planner hint only, not allocated)
├── maintenance_work_mem   see §3.3
├── work_mem × est. active sessions × avg. hash/sort ops per query
└── WAL buffers            wal_buffers = -1 (auto = 1/32 of shared_buffers, cap 16 MB)
```

**Hard rule:** `shared_buffers + (maintenance_work_mem × autovacuum_max_workers) + (work_mem × max_connections × 2)` must be < 80% of RAM. Migrations frequently OOM because `maintenance_work_mem` is set high for the loader session *and* autovacuum workers each grab the same amount.

### 2.3 Session, network, and lock hygiene

- [ ] Loader user has its own role; set session-level GUCs via `ALTER ROLE loader SET ...` so you don't perturb OLTP sessions.
- [ ] `statement_timeout = 0` and `idle_in_transaction_session_timeout = 0` **for the loader session only**.
- [ ] `lock_timeout` set (e.g., `30s`) to fail fast on schema-change contention.
- [ ] pgbouncer / RDS Proxy in transaction pool mode is *disabled* or bypassed for the loader — `COPY` and long transactions do not play well with transaction pooling.
- [ ] Verified `wal_level = replica` (or `logical` if downstream consumers exist). Do not silently drop to `minimal` on RDS; RDS forces `replica` for backup integrity.
- [ ] Backup retention window and any AWS Backup vaults confirmed — you cannot disable automated backups on Multi-AZ without extra work.
- [ ] DMS or logical replication slot lag alarm in place if using CDC; unbounded slots will fill the disk regardless of `max_wal_size`.

### 2.4 Baseline capture (run before changing anything)

```sql
-- Snapshot at t-0, then again post-cutover
SELECT now(), * FROM pg_stat_bgwriter;
SELECT now(), * FROM pg_stat_wal;                  -- PG14+
SELECT * FROM pg_stat_database WHERE datname = current_database();
SELECT * FROM pg_settings
 WHERE name IN ('checkpoint_timeout','max_wal_size','min_wal_size',
                'checkpoint_completion_target','maintenance_work_mem',
                'wal_compression','synchronous_commit','wal_buffers',
                'autovacuum','autovacuum_max_workers',
                'autovacuum_vacuum_cost_limit','autovacuum_vacuum_cost_delay',
                'autovacuum_naptime');
```

Save output to your migration ticket. Enable `log_checkpoints = on` and `log_autovacuum_min_duration = 0` for the load window — these are cheap and invaluable in postmortem.

---

## 3. Recommended parameters for the load window

### 3.1 Checkpoints & WAL

| Parameter | Steady-state default | **Load window** | Rationale |
|---|---|---|---|
| `checkpoint_timeout` | `5min` | **`30min`** (up to `1h` for very heavy loads) | Larger interval → fewer post-checkpoint full-page writes, less WAL amplification ([Percona](https://www.percona.com/blog/importance-of-tuning-checkpoint-in-postgresql/), [Cybertec](https://www.cybertec-postgresql.com/en/checkpoint-distance-and-amount-of-wal/)). |
| `max_wal_size` | `1GB` | **`32–64GB`** (≥ 1 hr of WAL at peak) | Prevents `max_wal_size`-triggered checkpoints during load; leaves `checkpoint_timeout` as the pacing knob ([The Build](https://thebuild.com/blog/a-little-more-on-max_wal_size/), [Crunchy](https://www.crunchydata.com/blog/tuning-your-postgres-database-for-high-write-loads)). |
| `min_wal_size` | `80MB` | **`2–4GB`** | Keeps recycled segments hot; avoids allocation churn during spikes ([Crunchy](https://www.crunchydata.com/blog/tuning-your-postgres-database-for-high-write-loads)). |
| `checkpoint_completion_target` | `0.9` | **`0.9`** (leave alone; consider `(timeout − 2min)/timeout` for very long timeouts) | Spreads I/O across the interval ([Percona](https://www.percona.com/blog/importance-of-tuning-checkpoint-in-postgresql/)). |
| `wal_compression` | `off` (older) / `on` | **`on`** (`lz4` on PG15+ if available) | Compresses full-page images; large win on load workloads. |
| `wal_buffers` | `-1` (auto) | **`64MB`** explicit | Cheap; reduces `WALWrite` waits under concurrency. |
| `synchronous_commit` | `on` | **`off` at session level for the loader** (never globally on Multi-AZ) | Lets COPY/INSERT batches acknowledge before WAL fsync; small durability window is acceptable for restartable loads. Do **not** use `local` or `off` cluster-wide on Multi-AZ. |
| `full_page_writes` | `on` | **`on` — do not disable** | Disabling risks torn pages after a crash ([mydbanotebook](https://mydbanotebook.org/posts/stop-punishing-your-postgres-for-a-crash-that-wont-happen/)). |

**Design principle:** you want checkpoints to be triggered by `checkpoint_timeout`, not by `max_wal_size`. If `pg_stat_bgwriter.checkpoints_req` (requested) exceeds `checkpoints_timed` during the load, raise `max_wal_size` further ([The Build](https://thebuild.com/blog/a-little-more-on-max_wal_size/)).

### 3.2 Load-time write path

| Parameter | Load window value | Notes |
|---|---|---|
| `commit_delay` | `10000` (µs) | Only helps when many concurrent small commits; skip for single-writer COPY. |
| `commit_siblings` | `5` | Companion to above. |
| `bgwriter_lru_maxpages` | `1000` | Keeps background writer flushing ahead of backends. |
| `bgwriter_delay` | `50ms` | More frequent background flushes. |

### 3.3 `maintenance_work_mem`

Used by `CREATE INDEX`, `VACUUM`, `ALTER TABLE ... ADD FOREIGN KEY` validation, and autovacuum workers. Post-load index creation is often the longest single phase of a migration.

| Instance RAM | Load window `maintenance_work_mem` | Autovacuum override |
|---|---|---|
| 16 GB | `1 GB` | `autovacuum_work_mem = 256MB` |
| 32 GB | `2 GB` | `autovacuum_work_mem = 512MB` |
| 64 GB | `4 GB` | `autovacuum_work_mem = 1GB` |
| 128 GB+ | `8 GB` | `autovacuum_work_mem = 1–2GB` |

**Critical:** set `autovacuum_work_mem` **explicitly** to a smaller value. Otherwise every autovacuum worker inherits `maintenance_work_mem`, and `autovacuum_max_workers × maintenance_work_mem` can exhaust RAM.

Set at the session level for parallel index builds:

```sql
SET maintenance_work_mem = '4GB';
SET max_parallel_maintenance_workers = 4;   -- PG11+
CREATE INDEX CONCURRENTLY ... ;             -- or non-CONCURRENTLY if load is offline
```

### 3.4 Autovacuum during load

Two valid strategies:

**Strategy A — Suspend autovacuum for the load window** (offline cutover, then explicit ANALYZE):

```sql
ALTER TABLE staging.big_table SET (autovacuum_enabled = false);
-- ...load...
ALTER TABLE staging.big_table RESET (autovacuum_enabled);
VACUUM (ANALYZE, VERBOSE) staging.big_table;
```

Use when the load is a single large `COPY` with no concurrent OLTP.

**Strategy B — Aggressive autovacuum** (concurrent OLTP or CDC replay):

| Parameter | Default | Load window | Reason |
|---|---|---|---|
| `autovacuum_max_workers` | `3` | **`5–6`** | More parallelism for many tables. |
| `autovacuum_naptime` | `1min` | **`15s`** | Wakes sooner to pick up newly-bloated tables ([OneUptime](https://oneuptime.com/blog/post/2026-02-17-how-to-optimize-autovacuum-settings-for-high-write-cloud-sql-postgresql-databases/view)). |
| `autovacuum_vacuum_cost_limit` | `200` (via `vacuum_cost_limit`) | **`2000`** | Raises I/O budget; autovacuum keeps up with high-write workloads ([EDB](https://www.enterprisedb.com/blog/autovacuum-tuning-basics), [AWS](https://aws.amazon.com/blogs/database/a-case-study-of-tuning-autovacuum-in-amazon-rds-for-postgresql/)). |
| `autovacuum_vacuum_cost_delay` | `2ms` (PG12+) | **`0` or `1ms`** | Removes throttling; only appropriate if IOPS headroom exists ([Azure docs](https://learn.microsoft.com/en-us/azure/postgresql/troubleshoot/how-to-autovacuum-tuning)). |
| `autovacuum_vacuum_scale_factor` | `0.2` | **`0.05`** globally, **`0.01`** on hot tables | Triggers vacuum after ~5% (or 1%) of the table is dead, not 20% ([EDB](https://www.enterprisedb.com/blog/autovacuum-tuning-basics)). |
| `autovacuum_vacuum_threshold` | `50` | **`10000`** | Damps runaway autovacuum on tiny tables. |
| `autovacuum_freeze_max_age` | `200M` | **`400M–800M`** | Delays anti-wraparound autovacuum during the load ([pganalyze](https://pganalyze.com/blog/5mins-postgres-tuning-vacuum-autovacuum), [Snowflake](https://www.snowflake.com/en/blog/engineering/tuning-postgres-vacuum/)). Do not exceed 1B. |
| `autovacuum_vacuum_insert_scale_factor` (PG13+) | `0.2` | **`0.05`** | Insert-only tables still need vacuum for visibility maps and freezing. |

Per-table overrides beat globals for hot tables:

```sql
ALTER TABLE public.orders SET (
  autovacuum_vacuum_scale_factor = 0.01,
  autovacuum_vacuum_cost_limit   = 2000,
  autovacuum_vacuum_cost_delay   = 0,
  autovacuum_vacuum_insert_scale_factor = 0.02
);
```

---

## 4. Crash-recovery impact matrix

Recovery after a crash replays WAL from the last successful checkpoint forward. Recovery duration is a function of *bytes of WAL to replay*, not of the setting values in isolation ([mydbanotebook](https://mydbanotebook.org/posts/stop-punishing-your-postgres-for-a-crash-that-wont-happen/), [Percona](https://www.percona.com/blog/importance-of-tuning-checkpoint-in-postgresql/)). Typical replay rate on a modern instance is 60–150 MB/s.

| Parameter | Change | WAL generated | Crash recovery time | Steady-state write throughput | Notes |
|---|---|---|---|---|---|
| `checkpoint_timeout` ↑ (5m → 30m) | ↓ ~40–80% (fewer FPI bursts) | ↑ moderate (more WAL to replay per crash, but less WAL total) | ↑↑ | Dominant lever. |
| `max_wal_size` ↑ (1G → 32G) | ↓ (fewer forced checkpoints) | ↑ up to ~one checkpoint cycle of WAL | ↑ | Soft cap; recovery bounded by whichever fires first (timeout vs. size) ([The Build](https://thebuild.com/blog/a-little-more-on-max_wal_size/)). |
| `checkpoint_completion_target` ↑ (0.5 → 0.9) | ≈ unchanged | ↑ slight (more WAL retained) | ↑ (smoother I/O) | Recommended stay at 0.9. |
| `wal_compression` on | ↓ 30–70% for FPI-heavy loads | ↓ (less WAL to read) — recovery often *faster* | ↑ | Small CPU cost. |
| `synchronous_commit` off (session) | ≈ unchanged | ≈ unchanged | ↑↑ | Loses last ~200ms of committed txns on crash. Loader-only, restartable loads. |
| `full_page_writes` off | ↓↓ | **Data corruption risk after crash** | ↑↑ | **Never do this.** |
| `fsync` off | ↓ | **Total corruption on crash** | ↑↑ | **Never do this.** |
| `maintenance_work_mem` ↑ | none | none | ↑ (index builds) | No recovery impact. |
| `autovacuum_*` aggressive | none directly | none | ≈ | Faster bloat reclamation → sustained throughput. |

**Rule of thumb:** with `checkpoint_timeout = 30min` and typical write rates, expect a few minutes of crash recovery on a modern RDS instance. On Multi-AZ, failover to the standby completes before recovery would ([Percona](https://www.percona.com/blog/importance-of-tuning-checkpoint-in-postgresql/), [mydbanotebook](https://mydbanotebook.org/posts/stop-punishing-your-postgres-for-a-crash-that-wont-happen/)). If you do not have a standby, cap `max_wal_size` more conservatively (16 GB) to bound the worst case.

---

## 5. Monitoring Performance Insights during the load

Performance Insights displays **DB load in Average Active Sessions (AAS)**, sliced by wait event. The `vCPU` reference line is your CPU capacity — sustained AAS above it means either CPU saturation or a wait event backlog.

### 5.1 Wait event cheat sheet

| Wait event | Category | What it means | First response |
|---|---|---|---|
| `LWLock:WALWrite` | Write-Ahead Log | Backends waiting for another to finish writing WAL | Raise `wal_buffers`; verify EBS bandwidth; batch commits. |
| `LWLock:WALInsert` | Write-Ahead Log | Contention on WAL insertion slots | Reduce concurrent writers or use fewer, larger transactions ([Rich Yen](https://richyen.com/postgres/2026/04/13/wait_events.html)). |
| `IO:WALSync` / `IO:WALWrite` | I/O | Fsync/write to WAL disk is slow | Provisioned IOPS check; consider `synchronous_commit=off` at session level. |
| `IO:XactSync` | I/O | Commit waiting for WAL flush | Batch commits into larger transactions; check network/IOPS ([AWS docs](https://docs.aws.amazon.com/AmazonRDS/latest/AuroraUserGuide/apg-waits.xactsync.html)). |
| `IO:DataFileWrite` / `IO:DataFileExtend` | I/O | Backend flushing/extending data files | Raise `shared_buffers`; more aggressive `bgwriter`. |
| `LWLock:BufferMapping` / `LWLock:BufferContent` | Buffers | Shared-buffer hotspot | Add indexes; partition; check hot-tuple update patterns. |
| `Lock:transactionid` / `Lock:tuple` | Client locks | Row-level contention | Reduce transaction size; retry logic. |
| `Lock:relation` | Client locks | DDL blocking DML (or vice versa) | Serialize schema changes; use `lock_timeout`. |
| `LWLock:ProcArray` | Concurrency | Too many connections | Add pgbouncer or RDS Proxy. |
| `CPU` (the green band) | CPU | Actual on-CPU work | Query plans, batch sizes, parallelism. |
| `IO:DataFileRead` | I/O | Cache miss on read | Larger `shared_buffers`; warm cache pre-load. |
| `Timeout:VacuumDelay` | Vacuum | Cost-based delay firing | Lower `autovacuum_vacuum_cost_delay`. |

### 5.2 Step-by-step: monitoring during a bulk load

**Every step below is done in the RDS console → Performance Insights, with a matching CloudWatch dashboard open in a second tab.**

1. **T-30 min — Establish baseline.**
   - Set PI time window to the last 1 hour.
   - Note steady-state AAS (usually < 1) and dominant wait events.
   - Confirm CloudWatch metrics visible: `WriteIOPS`, `WriteThroughput`, `WriteLatency`, `WALDiskUsage` (if PG13+), `TransactionLogsDiskUsage`, `FreeableMemory`, `CPUUtilization`, `EBSByteBalance%` (for gp2/gp3 burst).

2. **T-0 — Start load.**
   - Kick off `COPY` or DMS full-load task.
   - Switch PI to a **5-minute** window; watch AAS climb.

3. **T+2 min — First triage.**
   - Identify top wait event. Expected pattern for a healthy bulk load: `CPU` and `IO:DataFileWrite` share the load; `Client:ClientRead` appears when COPY streams from the client.
   - **Red flag:** `LWLock:WALWrite` or `LWLock:WALInsert` in top 3. Action: reduce concurrent writers, increase batch size, verify `wal_buffers = 64MB`, confirm `wal_compression = on`.

4. **T+5–15 min — Watch for checkpoint storms.**
   - In CloudWatch, look for a sawtooth on `WriteIOPS` and spikes on `WriteLatency` every ~5 min. That means `max_wal_size` is too low and checkpoints are firing on volume, not time.
   - Run `SELECT * FROM pg_stat_bgwriter;` — if `checkpoints_req > checkpoints_timed`, raise `max_wal_size`. This parameter is dynamic on RDS (no reboot) — bump it by 2× and reload.

5. **T+15 min — CPU thrash check.**
   - If AAS on `CPU` sits ≥ 1.5× `vCPU` line for > 5 min, you are thrashing. Common causes:
     - Too many parallel COPY workers → reduce to `min(vCPU, 8)`.
     - Trigger firing per row → disable non-essential triggers during load: `ALTER TABLE ... DISABLE TRIGGER USER;`
     - Foreign-key validation on every row → drop FKs before load, re-add `NOT VALID` after, then `VALIDATE CONSTRAINT` in a separate step.
     - Autovacuum running with `cost_delay=0` and no headroom → temporarily raise `autovacuum_vacuum_cost_delay` back to `2ms`.

6. **T+30 min — WAL disk check.**
   - CloudWatch `TransactionLogsDiskUsage` should plateau after one full `max_wal_size` cycle.
   - If it grows unbounded, a replication slot is holding WAL. Query:
     ```sql
     SELECT slot_name, active, restart_lsn,
            pg_size_pretty(pg_wal_lsn_diff(pg_current_wal_lsn(), restart_lsn)) AS lag
       FROM pg_replication_slots;
     ```
     Drop or catch up the offending slot before the disk fills.

7. **T+hourly — Long-running query & lock snapshot.**
   ```sql
   SELECT pid, now()-xact_start AS xact_age, state, wait_event_type, wait_event,
          left(query,120) AS query
     FROM pg_stat_activity
    WHERE state <> 'idle'
    ORDER BY xact_start;
   ```

8. **Load completion — Post-load actions.**
   - `VACUUM (ANALYZE, VERBOSE)` on loaded tables. Do **not** rely on autovacuum for the first pass; explicit ANALYZE gives the planner statistics immediately.
   - Rebuild/create indexes (`CREATE INDEX CONCURRENTLY` if there is concurrent traffic, otherwise plain `CREATE INDEX` — up to 10× faster with `max_parallel_maintenance_workers`).
   - Re-validate foreign keys: `ALTER TABLE ... VALIDATE CONSTRAINT ...;`
   - Reset parameters (see §6).

9. **T+24 h — Bloat and stats check.**
   ```sql
   SELECT schemaname, relname, n_live_tup, n_dead_tup,
          round(100.0 * n_dead_tup / NULLIF(n_live_tup + n_dead_tup,0),2) AS dead_pct,
          last_autovacuum, last_autoanalyze
     FROM pg_stat_user_tables
    ORDER BY n_dead_tup DESC
    LIMIT 20;
   ```

### 5.3 Alarm thresholds (CloudWatch)

| Metric | Warn | Page |
|---|---|---|
| `CPUUtilization` | > 80% for 10 min | > 90% for 5 min |
| `WriteLatency` | > 20 ms for 5 min | > 50 ms for 5 min |
| `TransactionLogsDiskUsage` | > 60% of storage | > 80% |
| `FreeableMemory` | < 15% of RAM | < 8% |
| `EBSByteBalance%` | < 50% | < 20% |
| `ReplicaLag` (Multi-AZ / read replica) | > 30 s | > 120 s |

---

## 6. Reset to steady state

After migration completes and the app is healthy, revert **checkpoint and autovacuum aggressiveness** back to production values. Leave `wal_compression`, `wal_buffers`, and `min_wal_size` at the tuned values — they are strictly beneficial.

```sql
-- Example post-migration values for a 32 GB instance under normal OLTP
ALTER SYSTEM SET checkpoint_timeout               = '15min';
ALTER SYSTEM SET max_wal_size                     = '8GB';
ALTER SYSTEM SET min_wal_size                     = '2GB';
ALTER SYSTEM SET checkpoint_completion_target     = '0.9';
ALTER SYSTEM SET wal_compression                  = 'on';
ALTER SYSTEM SET wal_buffers                      = '64MB';
ALTER SYSTEM SET maintenance_work_mem             = '512MB';
ALTER SYSTEM SET autovacuum_work_mem              = '256MB';
ALTER SYSTEM SET autovacuum_max_workers           = '3';
ALTER SYSTEM SET autovacuum_naptime               = '30s';
ALTER SYSTEM SET autovacuum_vacuum_cost_limit     = '1000';
ALTER SYSTEM SET autovacuum_vacuum_cost_delay     = '2ms';
ALTER SYSTEM SET autovacuum_vacuum_scale_factor   = '0.1';
SELECT pg_reload_conf();
```

On RDS, `ALTER SYSTEM` is blocked — apply these through the **DB parameter group** attached to the instance. Static parameters (e.g., `shared_buffers`) require a reboot; dynamic ones (all of the above) apply on reload.

---

## 7. Common failure modes and remediations

| Symptom | Likely cause | Fix |
|---|---|---|
| WAL disk filling despite large `max_wal_size` | Orphaned replication slot | Drop slot: `SELECT pg_drop_replication_slot(...)` |
| Bulk load throughput collapses every ~5 min | `max_wal_size` too small → forced checkpoints | Raise `max_wal_size` 2–4× |
| PI dominated by `IO:XactSync` | Small, frequent commits | Batch into larger transactions; `synchronous_commit=off` on loader session |
| CPU 100%, PI shows mostly green (CPU) band | Trigger/FK validation per row; missing index on FK parent | Disable triggers; add supporting index; use `NOT VALID` FKs |
| Post-load queries slow, `pg_stat_user_tables` shows no `last_analyze` | Autovacuum suspended and never ran | Run `VACUUM (ANALYZE)` explicitly |
| `LWLock:BufferMapping` dominant | Hot tuple / hot page | Partition table; use `HOT` updates; batch by non-hot key |
| Multi-AZ replica lag climbs | Sync replication amplifying WAL fsync cost | Verify network; consider brief conversion to Single-AZ during load (RPO risk — coordinate with stakeholders) |
| OOM kill of backend during index build | `maintenance_work_mem × autovacuum_max_workers` too large | Set `autovacuum_work_mem` explicitly, lower than `maintenance_work_mem` |

---

## 8. References

- [PostgreSQL: WAL Configuration (official docs)](https://www.postgresql.org/docs/current/wal-configuration.html)
- [PostgreSQL: Automatic Vacuuming (official docs)](https://www.postgresql.org/docs/current/runtime-config-autovacuum.html)
- [AWS: Tuning with wait events for RDS PostgreSQL](https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/PostgreSQL.Tuning.html)
- [AWS: RDS for PostgreSQL wait events reference](https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/PostgreSQL.Tuning.concepts.summary.html)
- [AWS: IO:XactSync wait event](https://docs.aws.amazon.com/AmazonRDS/latest/AuroraUserGuide/apg-waits.xactsync.html)
- [AWS blog: A Case Study of Tuning Autovacuum in Amazon RDS for PostgreSQL](https://aws.amazon.com/blogs/database/a-case-study-of-tuning-autovacuum-in-amazon-rds-for-postgresql/)
- [Percona: Importance of Tuning Checkpoint in PostgreSQL](https://www.percona.com/blog/importance-of-tuning-checkpoint-in-postgresql/)
- [Percona: Tuning Autovacuum in PostgreSQL and Autovacuum Internals](https://www.percona.com/blog/tuning-autovacuum-in-postgresql-and-autovacuum-internals/)
- [Cybertec: Checkpoint distance and amount of WAL](https://www.cybertec-postgresql.com/en/checkpoint-distance-and-amount-of-wal/)
- [Crunchy Data: Tuning Your Postgres Database for High Write Loads](https://www.crunchydata.com/blog/tuning-your-postgres-database-for-high-write-loads)
- [The Build: A little more on max_wal_size](https://thebuild.com/blog/a-little-more-on-max_wal_size/)
- [mydbanotebook: Stop Punishing Your Postgres for a Crash That Won't Happen](https://mydbanotebook.org/posts/stop-punishing-your-postgres-for-a-crash-that-wont-happen/)
- [EDB: Autovacuum Tuning Basics for Optimizing Performance](https://www.enterprisedb.com/blog/autovacuum-tuning-basics)
- [Richard Yen: Understanding PostgreSQL Wait Events](https://richyen.com/postgres/2026/04/13/wait_events.html)
- [OneUptime: How to Load Millions of Rows with COPY in PostgreSQL](https://oneuptime.com/blog/post/2026-01-25-load-millions-rows-copy-postgresql/view)
- [pganalyze: Basics of tuning VACUUM and autovacuum](https://pganalyze.com/blog/5mins-postgres-tuning-vacuum-autovacuum)
