# 2 TB RDS PostgreSQL — Backfill Runbook (Companion to Zero-Downtime DDL Playbook)

This runbook operationalizes Section 1 (Additive Migrations) and Section 5 (Cleanup Deprecated Columns) of the [Zero-Downtime DDL Playbook](./zero_downtime_ddl_playbook.md) for a **2 TB fleet with logical replication**. Every value below is derived from the actual constraints of that scale, not generic advice.

---

## 0. Sizing Assumptions

| Parameter | Value | Why |
|---|---|---|
| Publisher size | ~2 TB | Baseline |
| Peak write rate | 500 – 2 000 rows/s | Governs chunk size |
| Publisher instance class | `db.r6g.4xlarge` (16 vCPU, 128 GB RAM) | Typical for 2 TB OLTP |
| WAL generation | 30 – 80 MB/min steady | Governs slot-retention headroom |
| Replay-lag SLO | **≤ 1 GB** at all times | Green build assertion 3 |
| Free storage headroom | ≥ 400 GB (20% of 2 TB) | Buffer for WAL bloat |
| Backfill window | Any time; work is chunked | No maintenance window needed |

---

## 1. RDS Parameter Group — Tuned Values

Set on the **custom parameter group** for the publisher (reboot NOT required for most; those that do are marked ⚠️):

```
# --- Logical replication ---
wal_level                = logical
max_wal_senders          = 20              # 3× your slot count for headroom
max_replication_slots    = 20              # same
wal_sender_timeout       = 60000           # 60s — matches subscriber apply timeout
max_slot_wal_keep_size   = 65536           # 64 GB — hard ceiling before slot is invalidated
wal_keep_size            = 4096            # 4 GB — safety net for physical replicas

# --- Lock/timeout hygiene ---
lock_timeout             = 2000            # 2s per statement; matches harness assertion 1
statement_timeout        = 0               # keep 0 globally; per-migration override via SET LOCAL
idle_in_transaction_session_timeout = 60000  # 60s — kills forgotten backfill txns

# --- Vacuum for backfill churn ---
autovacuum_vacuum_scale_factor  = 0.05     # trigger sooner on wide updates
autovacuum_analyze_scale_factor = 0.02
autovacuum_vacuum_cost_limit    = 2000     # 4× default; keep up with backfill churn
autovacuum_naptime              = 10s
maintenance_work_mem            = 2GB      # index rebuilds during backfill

# --- Statement-level backfill throughput ---
work_mem                        = 32MB     # chunk queries stay in memory
synchronous_commit              = on       # do NOT flip to off during backfill — parity risk
```

⚠️ `max_wal_senders`, `max_replication_slots`, `wal_level` require a reboot.

Verify with:
```sql
SELECT name, setting, unit, boot_val
FROM pg_settings
WHERE name IN (
  'wal_sender_timeout','max_slot_wal_keep_size','lock_timeout',
  'autovacuum_vacuum_scale_factor','maintenance_work_mem'
);
```

---

## 2. Backfill Sizing — First Principles

The three constraints in tension:

1. **Chunk size × write rate must not exceed replay budget.** For a 1 GB replay budget and 40 MB/min steady WAL, a single chunk that generates > 500 MB of WAL will breach the SLO.
2. **Chunk duration must stay under `lock_timeout`.** With `lock_timeout = 2000`, an UPDATE that takes > 2 s on any row will error out and roll back the entire chunk.
3. **Chunk cadence must let autovacuum keep up.** If churn generates 100k dead tuples/minute and autovacuum runs every 10 s at cost-limit 2000, you can sustain roughly 6–8k tuple deletions per autovacuum cycle.

### Recommended defaults for 2 TB fleet

| Table cardinality | Chunk size | Sleep between chunks |
|---|---|---|
| < 10 M rows | 10 000 | 100 ms |
| 10 M – 100 M | 5 000 | 250 ms |
| 100 M – 500 M | 2 000 | 500 ms |
| > 500 M | 1 000 | 1 000 ms |

Adjust up/down based on the **replay-lag headroom** observed in the first 5 minutes of a backfill (see §5 abort criteria).

---

## 3. Backfill Progress Table

Every backfill writes to a bookkeeping table on the publisher. This is what makes backfills **resumable, observable, and abortable**.

```sql
CREATE TABLE IF NOT EXISTS ops.backfill_progress (
    backfill_id      text PRIMARY KEY,               -- e.g. 'users_email_verified_20260615'
    target_table     text NOT NULL,
    target_column    text,
    started_at       timestamptz NOT NULL DEFAULT now(),
    finished_at      timestamptz,
    last_id_processed bigint,                        -- watermark for resume
    total_rows       bigint,
    rows_done        bigint NOT NULL DEFAULT 0,
    chunk_size       int    NOT NULL,
    sleep_ms         int    NOT NULL,
    status           text   NOT NULL DEFAULT 'running'
                     CHECK (status IN ('running','paused','done','aborted','failed')),
    aborted_reason   text,
    updated_at       timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS backfill_progress_status_idx
    ON ops.backfill_progress (status, updated_at DESC);
```

Add the corresponding row to the **📋 DDL Change Log** in Notion (auto-populated by CI at DDL time).

---

## 4. The Chunked Backfill Loop

The canonical pattern. Replace `<column>`, `<expr>`, and `<table>` for the specific backfill. **Do not deviate from this shape** — the abort semantics and lock/lag hygiene depend on it.

```sql
DO $$
DECLARE
  _backfill_id text := 'users_email_verified_20260615';
  _batch       int  := 5000;
  _sleep_ms    int  := 250;
  _last_id     bigint;
  _rows        int;
  _target_rows bigint;
BEGIN
  -- 4.1 Initialize progress row (idempotent)
  INSERT INTO ops.backfill_progress
    (backfill_id, target_table, target_column, chunk_size, sleep_ms,
     total_rows, last_id_processed)
  VALUES
    (_backfill_id, 'public.users', 'email_verified_at', _batch, _sleep_ms,
     (SELECT count(*) FROM public.users WHERE email_verified_at IS NULL),
     0)
  ON CONFLICT (backfill_id) DO UPDATE SET
     status = 'running', updated_at = now();

  SELECT last_id_processed INTO _last_id
    FROM ops.backfill_progress WHERE backfill_id = _backfill_id;

  LOOP
    -- 4.2 Abort switch: check the progress row (allows external kill)
    IF (SELECT status FROM ops.backfill_progress
        WHERE backfill_id = _backfill_id) <> 'running' THEN
       RAISE NOTICE 'backfill % status is not running; exiting', _backfill_id;
       EXIT;
    END IF;

    -- 4.3 One chunk, its own txn, own lock_timeout
    BEGIN
      -- Per-statement guard (belt + suspenders vs param group)
      PERFORM set_config('lock_timeout',     '2000', true);
      PERFORM set_config('statement_timeout','60000', true);

      WITH batch AS (
        SELECT id
        FROM public.users
        WHERE id > _last_id
          AND email_verified_at IS NULL      -- only backfill the target rows
        ORDER BY id
        LIMIT _batch
        FOR UPDATE SKIP LOCKED               -- coexist with live writes
      )
      UPDATE public.users u
         SET email_verified_at = COALESCE(u.email_verified_at, u.created_at)
        FROM batch b
       WHERE u.id = b.id
      RETURNING u.id INTO _last_id;

      GET DIAGNOSTICS _rows = ROW_COUNT;

      UPDATE ops.backfill_progress
         SET last_id_processed = _last_id,
             rows_done         = rows_done + _rows,
             updated_at        = now()
       WHERE backfill_id = _backfill_id;
    EXCEPTION WHEN OTHERS THEN
      UPDATE ops.backfill_progress
         SET status = 'failed', aborted_reason = SQLERRM, updated_at = now()
       WHERE backfill_id = _backfill_id;
      RAISE;
    END;

    EXIT WHEN _rows = 0;

    -- 4.4 Yield to autovacuum and replication apply
    PERFORM pg_sleep(_sleep_ms / 1000.0);
  END LOOP;

  UPDATE ops.backfill_progress
     SET status = 'done', finished_at = now(), updated_at = now()
   WHERE backfill_id = _backfill_id;
END $$;
```

### Why every piece exists

| Line | Purpose |
|---|---|
| `PRIMARY KEY (id) FOR UPDATE SKIP LOCKED` | Coexists with foreground writes; never blocks a user request |
| `id > _last_id` + `ORDER BY id LIMIT` | Keyset pagination — never re-scans the whole table |
| `WHERE email_verified_at IS NULL` | Skips already-migrated rows on resume |
| `set_config('lock_timeout', ..., true)` | Overrides GUC per-transaction (survives even if param group is wrong) |
| External `status` check | Lets you `UPDATE backfill_progress SET status='paused'` from any session to stop the loop cleanly |
| Per-chunk BEGIN…EXCEPTION | One chunk failing does not roll back progress accounting |
| `pg_sleep(_sleep_ms/1000.0)` | Rate limiter — the single biggest lever on replay-lag budget |

---

## 5. Abort / Resume Semantics

### To pause
```sql
UPDATE ops.backfill_progress
   SET status = 'paused', updated_at = now()
 WHERE backfill_id = 'users_email_verified_20260615';
```
The loop exits at the **top** of its next iteration — mid-chunk work either completes or rolls back atomically. No partial updates.

### To resume
```sql
UPDATE ops.backfill_progress
   SET status = 'running', updated_at = now()
 WHERE backfill_id = 'users_email_verified_20260615';
```
Then re-run the DO block. It picks up from `last_id_processed`.

### Automatic abort criteria — configure the CI harness / cron to enforce

Fail the run and set `status = 'aborted'` if **any** of the following are true for 60 s consecutively:

| Metric | Ceiling |
|---|---|
| `pg_stat_replication.replay_lag` (bytes) | > 1 GB |
| `pg_replication_slots.retained_bytes` | > 32 GB (half of `max_slot_wal_keep_size`) |
| Any single chunk hitting `lock_timeout` | > 3 chunks in 5 min |
| Publisher CPU utilization | > 85% for 5 min |
| Publisher `FreeStorageSpace` | < 300 GB |

Kill switch (paste-ready):
```sql
UPDATE ops.backfill_progress
   SET status = 'aborted', aborted_reason = 'lag SLO breach', updated_at = now()
 WHERE status = 'running';
```

---

## 6. Backfill Dry-Run in CI

The staging test routine (Section 4 of the playbook) should include a **backfill dry-run stage** for any migration whose `Change Type ∈ {Backfill, Additive Column with default, Type Change}`:

```bash
# In ci/migration_tests/run.sh, add optional stage between steps 4 and 5
if [[ -f "$MIGRATION_DIR/backfill.sql" ]]; then
  log INFO "backfill dry-run"
  BACKFILL_START=$(date +%s)
  psql "$PUB_URI" -v ON_ERROR_STOP=1 -f "$MIGRATION_DIR/backfill.sql"
  BACKFILL_S=$(( $(date +%s) - BACKFILL_START ))
  log INFO "backfill dry-run completed in ${BACKFILL_S}s"
  # Replication catchup already asserted downstream at step 5
fi
```

`backfill.sql` should be a scaled-down version of the production loop (typically 1–5% of production row count, same chunk_size / sleep_ms ratio).

---

## 7. Post-Backfill Verification

Before flipping the "cleanup phase P0 done" checkbox in the DDL Change Log, confirm:

```sql
-- 7.1 Row count parity between publisher and subscriber
SELECT count(*) AS pub_count FROM public.users WHERE email_verified_at IS NULL;
-- run same on subscriber, expect same value (usually 0 after full backfill)

-- 7.2 No rows still on default value where they shouldn't be
SELECT count(*) AS unmigrated FROM public.users WHERE email_verified_at IS NULL;

-- 7.3 Slot has caught back up
SELECT slot_name, pg_size_pretty(pg_wal_lsn_diff(pg_current_wal_lsn(), confirmed_flush_lsn)) AS lag
FROM pg_replication_slots;

-- 7.4 No long-held locks
SELECT pid, mode, relation::regclass, age(now(), query_start)
FROM pg_locks JOIN pg_stat_activity USING (pid)
WHERE granted AND relation IS NOT NULL AND mode LIKE '%Exclusive%';
```

All four must be clean before proceeding to Cleanup Phase P1 (Prove-No-Reader) of the playbook.

---

## 8. Reference Card

| Task | Command |
|---|---|
| List active backfills | `SELECT * FROM ops.backfill_progress WHERE status='running';` |
| Pause all | `UPDATE ops.backfill_progress SET status='paused' WHERE status='running';` |
| See progress | `SELECT backfill_id, rows_done, total_rows, ROUND(100.0*rows_done/NULLIF(total_rows,0),1) AS pct FROM ops.backfill_progress;` |
| Slot lag right now | `SELECT slot_name, pg_size_pretty(pg_wal_lsn_diff(pg_current_wal_lsn(), confirmed_flush_lsn)) FROM pg_replication_slots;` |
| Kill the loudest offender | `SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE query LIKE '%backfill_progress%' AND state='active';` |

---

## Sources

- [PostgreSQL 16 — Chapter 30. Logical Replication](https://www.postgresql.org/docs/16/logical-replication.html)
- [PostgreSQL 16 — `max_slot_wal_keep_size`](https://www.postgresql.org/docs/16/runtime-config-replication.html)
- [PostgreSQL 16 — `SELECT ... FOR UPDATE SKIP LOCKED`](https://www.postgresql.org/docs/16/sql-select.html#SQL-FOR-UPDATE-SHARE)
- [AWS RDS User Guide — PostgreSQL logical replication](https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/CHAP_PostgreSQL.html#PostgreSQL.Concepts.General.FeatureSupport.LogicalReplication)
- [AWS RDS User Guide — DB parameter groups](https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/USER_WorkingWithParamGroups.html)
