# Zero-Downtime DDL Playbook

**Scope:** AWS RDS for PostgreSQL (v13+) with native logical replication (`rds.logical_replication = 1`) or `pglogical`. Applies to primary → logical subscriber(s), including blue/green stacks, cross-region replicas, and analytics fan-out subscribers.

**Core principle — Expand / Migrate / Contract.** Every breaking change is split into additive (expand) → backfill/dual-write → cutover → cleanup (contract). Never rename, retype, or drop in a single deploy.

**Golden rules**

1. Every DDL runs with an aggressive `lock_timeout` and a retry loop. No exceptions.
2. Logical replication does not carry DDL. Schema changes are applied to publisher **and** every subscriber, in an order that keeps the subscription running.
3. No `ACCESS EXCLUSIVE` operation runs while replication lag > threshold or long transactions are on the wire.
4. Deprecated objects go through a *tombstone* period (≥ 1 full release + backup cycle) before physical drop.

---

## 1. Checklist — Additive Schema Migrations

Use this before merging any migration PR. Every "yes" is required; a "no" blocks the change.

### 1.1 Design-time gates

- [ ] Change is **additive only** in this deploy (new column, new table, new index, new constraint `NOT VALID`, new enum value at end).
- [ ] No `ALTER COLUMN TYPE` that rewrites the table (see §1.4 safe/unsafe matrix).
- [ ] No `ADD COLUMN ... NOT NULL` **without** a constant/volatile-free `DEFAULT` on PG 11+, and never on PG ≤ 10.
- [ ] No `RENAME` of a column/table currently referenced by application code or by a logical publication.
- [ ] New columns are nullable **or** have an immutable default; application writes populate them from day one.
- [ ] Foreign keys and check constraints are added `NOT VALID` first, then `VALIDATE CONSTRAINT` in a follow-up.
- [ ] Indexes are built with `CREATE INDEX CONCURRENTLY`, one statement per migration file (cannot run in a transaction block).
- [ ] Enum additions use `ALTER TYPE ... ADD VALUE` (PG 12+ can run outside a transaction; PG < 12 requires standalone commit).
- [ ] For any table participating in a publication: change is compatible with `REPLICA IDENTITY` (do not drop the identity column or its index).

### 1.2 Application-code gates

- [ ] Old and new schemas are both readable by **N-1** and **N** app versions. A rollback of the app must not break against the migrated DB.
- [ ] ORM models regenerated; no `SELECT *` behavior would blow up on a new column.
- [ ] Feature flags gate any *write* to the new column until the migration is verified on all replicas.
- [ ] Read paths tolerate `NULL` in the new column during backfill.

### 1.3 Operational gates

- [ ] `lock_timeout` and `statement_timeout` explicitly set in the migration script (see §2).
- [ ] Migration is idempotent (`IF NOT EXISTS`, `DO $$ ... $$` guards) so retries are safe.
- [ ] Runbook entry with exact SQL, rollback SQL, expected duration, and an owner on call.
- [ ] Rehearsed on a staging clone restored from a recent prod snapshot (§4).
- [ ] Logical replication lag baseline captured; alerts tuned for the deploy window (§3).
- [ ] Change window avoids: nightly `VACUUM`/analytics batch jobs, RDS maintenance windows, autovacuum-to-prevent-wraparound windows.

### 1.4 Safe vs. unsafe DDL matrix (PG 13+)

| Operation | Lock | Table rewrite | Verdict |
|---|---|---|---|
| `ADD COLUMN` nullable, no default | ACCESS EXCLUSIVE (brief) | No | ✅ Safe |
| `ADD COLUMN` with **immutable** default (PG 11+) | ACCESS EXCLUSIVE (brief) | No | ✅ Safe |
| `ADD COLUMN` with volatile default (e.g. `now()`) | ACCESS EXCLUSIVE | Yes | ❌ Unsafe — split |
| `ADD COLUMN` with `NOT NULL` (no default) | ACCESS EXCLUSIVE | Full scan | ❌ Unsafe — add nullable, backfill, `SET NOT NULL` via `NOT VALID` check |
| `DROP COLUMN` | ACCESS EXCLUSIVE (brief) | No (logical only) | ⚠️ Only after tombstone period (§5) |
| `ALTER COLUMN TYPE` (binary-compatible, e.g. `varchar(100)` → `varchar(200)`) | ACCESS EXCLUSIVE (brief) | No | ✅ Safe |
| `ALTER COLUMN TYPE` requiring cast | ACCESS EXCLUSIVE | Yes | ❌ Unsafe — new column + backfill |
| `SET NOT NULL` (bare) | ACCESS EXCLUSIVE | Full scan | ❌ Unsafe — use `CHECK ... NOT VALID` → `VALIDATE` → `SET NOT NULL` (PG 12+ skips rescan) |
| `ADD CONSTRAINT FK/CHECK` | ACCESS EXCLUSIVE + full scan | No | ⚠️ Add `NOT VALID`, then `VALIDATE CONSTRAINT` (SHARE UPDATE EXCLUSIVE) |
| `CREATE INDEX` | SHARE (blocks writes) | No | ❌ Unsafe — use `CONCURRENTLY` |
| `CREATE INDEX CONCURRENTLY` | SHARE UPDATE EXCLUSIVE | No | ✅ Safe |
| `DROP INDEX` | ACCESS EXCLUSIVE | No | ⚠️ Use `DROP INDEX CONCURRENTLY` |
| `ALTER TYPE ... ADD VALUE` (enum) | ACCESS EXCLUSIVE (brief) | No | ✅ Safe (PG 12+) |
| `TRUNCATE` | ACCESS EXCLUSIVE | — | ❌ Never inside a hot table |
| `CLUSTER`, `VACUUM FULL`, `REINDEX TABLE` (non-concurrent) | ACCESS EXCLUSIVE | Yes | ❌ Use `REINDEX ... CONCURRENTLY` (PG 12+) |

---

## 2. Risk-Mitigation Workflow — Handling DDL Locks

Any DDL that needs `ACCESS EXCLUSIVE` must acquire the lock **fast or not at all**. A stuck `ALTER TABLE` sits at the head of the lock queue and blocks every subsequent query on that table — this is the single most common cause of "the deploy took the site down."

### 2.1 The universal safe-DDL wrapper

```sql
-- safe_ddl.sql — invoked once per migration statement
BEGIN;
SET LOCAL lock_timeout    = '2s';      -- do not wait more than 2s for the lock
SET LOCAL statement_timeout = '15s';   -- upper bound once we own the lock
SET LOCAL idle_in_transaction_session_timeout = '5s';

-- The actual DDL. Must be idempotent.
ALTER TABLE :"table" ADD COLUMN IF NOT EXISTS :"col" :"type";

COMMIT;
```

Run it under a retry loop so a `lock_timeout` error is a normal, recoverable event:

```bash
#!/usr/bin/env bash
# run_ddl.sh — retry safe_ddl.sql with exponential backoff
set -euo pipefail
: "${PGURI:?}"; : "${SQL_FILE:?}"

max_attempts=30
attempt=1
sleep_s=1
while (( attempt <= max_attempts )); do
  if psql "$PGURI" -v ON_ERROR_STOP=1 -X -q -f "$SQL_FILE"; then
    echo "[ok] ${SQL_FILE} on attempt ${attempt}"; exit 0
  fi
  echo "[retry ${attempt}] lock contention or timeout; sleeping ${sleep_s}s"
  sleep "$sleep_s"
  sleep_s=$(( sleep_s < 30 ? sleep_s * 2 : 30 ))
  attempt=$(( attempt + 1 ))
done
echo "[fail] gave up after ${max_attempts} attempts"; exit 1
```

Why this pattern: a bare `ALTER TABLE` will queue behind any long-running `SELECT`, and every new query then queues behind the `ALTER`, freezing the table. A short `lock_timeout` with retries lets normal traffic drain between attempts.

### 2.2 Pre-flight — kill the lock queue *before* firing DDL

```sql
-- 1. Are there long-running txns touching the table? (>30s)
SELECT pid, usename, application_name, state, wait_event_type, wait_event,
       xact_start, now() - xact_start AS xact_age, query
FROM pg_stat_activity
WHERE (query ILIKE '%<table_name>%' OR pid IN (
        SELECT pid FROM pg_locks WHERE relation = '<schema>.<table>'::regclass))
  AND state <> 'idle'
  AND now() - xact_start > interval '30 seconds'
ORDER BY xact_start;

-- 2. Are there existing locks on the target?
SELECT l.locktype, l.mode, l.granted, a.pid, a.usename, a.query,
       now() - a.xact_start AS xact_age
FROM pg_locks l
JOIN pg_stat_activity a USING (pid)
WHERE l.relation = '<schema>.<table>'::regclass
ORDER BY l.granted DESC, a.xact_start;

-- 3. Cancel (do not terminate) blockers that are safe to interrupt:
SELECT pg_cancel_backend(pid) FROM pg_stat_activity
WHERE state = 'idle in transaction'
  AND now() - state_change > interval '5 minutes';
```

Never `pg_terminate_backend` an application session mid-deploy without paging the owning service — you can drop uncommitted work.

### 2.3 Decision tree during the window

```
DDL attempt → lock_timeout hit?
  ├─ No → success, move on.
  └─ Yes →
       ├─ Retry count < N → sleep, retry.
       ├─ Retry count = N →
       │    ├─ Inspect pg_locks / pg_stat_activity
       │    ├─ Is blocker an autovacuum on this table?
       │    │     └─ Yes → `SELECT pg_cancel_backend(pid)` — autovacuum will reschedule.
       │    ├─ Is blocker a known batch job?
       │    │     └─ Yes → coordinate pause, then retry.
       │    └─ Otherwise → abort, roll back deploy, investigate.
       └─ Replication lag > threshold at any point → pause DDL, drain, resume (§3).
```

### 2.4 Special cases

- **`CREATE INDEX CONCURRENTLY`** cannot run in a transaction and does *not* obey `lock_timeout` in the same way (it takes `SHARE UPDATE EXCLUSIVE`, not `ACCESS EXCLUSIVE`). If it fails, the leftover index is marked `INVALID` — drop it with `DROP INDEX CONCURRENTLY` before retrying:

  ```sql
  SELECT c.relname AS index_name
  FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid
  WHERE NOT i.indisvalid;
  ```

- **`VALIDATE CONSTRAINT`** takes `SHARE UPDATE EXCLUSIVE` — safe against writes but conflicts with other DDL. Run in its own migration step.
- **Autovacuum-to-prevent-wraparound** holds `SHARE UPDATE EXCLUSIVE` and cannot be cancelled cleanly on very old tables. Check `pg_stat_all_tables.last_autovacuum` and `age(relfrozenxid)` before scheduling DDL on large legacy tables.
- **RDS `rds_superuser` is not superuser** — you cannot `SET session_replication_role = replica` to bypass triggers on the primary. Design migrations without that shortcut.

---

## 3. Managing Logical Replication Lag During Migrations

Logical replication decodes WAL on the publisher and ships row-level changes to subscribers. Three failure modes to design around:

1. **DDL is not replicated.** Schema drift between publisher and subscriber will crash the apply worker (`relation "..." does not exist` / `column "..." missing`).
2. **Large backfills generate WAL faster than subscribers can apply**, growing the replication slot and eventually filling the publisher's disk.
3. **A stalled or dropped slot means unbounded WAL retention** on the publisher.

### 3.1 Ordering rule for DDL under logical replication

For **additive** changes:

1. Apply DDL on **subscriber first** (add the column/table there).
2. Apply DDL on **publisher** second.
3. Application deploy that writes the new column follows.

For **destructive** changes (drop column, drop table) — reverse:

1. Application deploy stops writing/reading the object.
2. Drop on **publisher** first (so it stops appearing in the replication stream).
3. Drop on **subscriber** last.

For a new table that should be replicated:

```sql
-- publisher
CREATE TABLE new_table (...);
ALTER PUBLICATION my_pub ADD TABLE new_table;

-- subscriber (must exist with matching schema BEFORE the next refresh)
CREATE TABLE new_table (...);
ALTER SUBSCRIPTION my_sub REFRESH PUBLICATION WITH (copy_data = true);
```

### 3.2 Baseline monitoring queries

Run these on the **publisher** every 10s during a migration window; alert on the thresholds shown.

```sql
-- Slot lag in bytes + retained WAL
SELECT slot_name, active, database,
       pg_size_pretty(pg_wal_lsn_diff(pg_current_wal_lsn(), restart_lsn)) AS retained_wal,
       pg_wal_lsn_diff(pg_current_wal_lsn(), confirmed_flush_lsn) AS confirmed_lag_bytes
FROM pg_replication_slots
WHERE slot_type = 'logical'
ORDER BY confirmed_lag_bytes DESC NULLS LAST;

-- Per-subscriber apply state
SELECT application_name, client_addr, state, sync_state,
       pg_wal_lsn_diff(pg_current_wal_lsn(), sent_lsn)   AS send_lag_bytes,
       pg_wal_lsn_diff(sent_lsn, flush_lsn)              AS flush_lag_bytes,
       pg_wal_lsn_diff(flush_lsn, replay_lsn)            AS replay_lag_bytes,
       write_lag, flush_lag, replay_lag
FROM pg_stat_replication;

-- PG 14+: apply-worker level detail
SELECT * FROM pg_stat_replication_slots;
```

**Suggested thresholds** (tune to your RPO):

| Metric | Warn | Page | Hard stop |
|---|---|---|---|
| `retained_wal` | 1 GiB | 5 GiB | 20 GiB or 30% of `EBS` free |
| `replay_lag` | 30s | 2m | 10m |
| Slot `active = false` | any | any | any |

RDS-side signals to alarm on in CloudWatch: `OldestReplicationSlotLag`, `TransactionLogsDiskUsage`, `ReplicationSlotDiskUsage`, `FreeStorageSpace`.

### 3.3 Strategies to keep lag bounded during DDL/backfill

1. **Chunk the backfill.** Never `UPDATE ... WHERE new_col IS NULL` in one shot. Use keyset pagination, 1k–10k rows per batch, with `pg_sleep` between batches proportional to observed lag.

    ```sql
    -- backfill_chunk.sql, parameterized
    WITH cte AS (
      SELECT id FROM big_table
      WHERE id > :last_id AND new_col IS NULL
      ORDER BY id LIMIT 5000
    )
    UPDATE big_table t
    SET new_col = compute(t.*)
    FROM cte WHERE t.id = cte.id
    RETURNING max(t.id);
    ```

    Adaptive pacing pseudocode:

    ```python
    while True:
        lag = query_replay_lag_bytes()
        if lag > 500 * MB:  time.sleep(5); continue
        if lag > 100 * MB:  time.sleep(1)
        last_id = run_chunk(last_id)
        if last_id is None: break
    ```

2. **Split large publications.** One publication per hot table (or per logical domain) so a single backfilled table cannot starve unrelated subscribers.

3. **Prefer column filters (PG 15+) and row filters** on the publication to keep backfill volume off subscribers that do not need it.

4. **Increase subscriber parallelism (PG 16+):** `ALTER SUBSCRIPTION my_sub SET (streaming = parallel);` — commits large transactions in parallel apply workers instead of a single serial worker.

5. **Raise `wal_sender_timeout` / `wal_receiver_timeout`** to survive a slow apply spike without the slot going inactive; do not raise `max_slot_wal_keep_size` beyond what your disk can absorb — that's a foot-gun, not a fix.

6. **Snapshot the publisher's disk headroom before starting.** If `TransactionLogsDiskUsage + expected_backfill_wal > free_space * 0.7`, defer or scale storage first (RDS storage grows online but is rate-limited).

7. **If lag runs away:** pause the backfill driver, let subscribers catch up, then resume. Do **not** drop and recreate the slot — you will lose position and require a full resync.

### 3.4 Hot-path guardrails

- Never run a table rewrite (`ALTER COLUMN TYPE`, `CLUSTER`) on a published table — the entire rewrite becomes WAL that the subscriber must apply. If unavoidable, temporarily drop the table from the publication, rewrite, resync.
- Beware **`TRUNCATE`**: it is logically replicated (PG 11+) and takes an `ACCESS EXCLUSIVE` on both sides.
- Long transactions on the **publisher** hold the slot's `catalog_xmin`, bloating `pg_catalog` — enforce `idle_in_transaction_session_timeout` globally.

---

## 4. Scriptable Migration Testing Routine (Staging)

Goal: every migration is exercised end-to-end against a **production-shaped** dataset with logical replication live, before it can be tagged for prod.

### 4.1 Staging topology

```
prod-snapshot ──► staging-primary (publisher) ──logical──► staging-replica (subscriber)
                        │
                        └── synthetic load generator (pgbench + app replay)
```

Rebuild staging from the latest prod snapshot at least weekly. Enable `rds.logical_replication = 1` and a matching parameter group.

### 4.2 The test harness

Save as `migration_test.sh`. Runs unattended in CI against staging.

```bash
#!/usr/bin/env bash
# migration_test.sh — end-to-end verification for a single migration
set -euo pipefail

: "${PUB_URI:?}"          # staging publisher
: "${SUB_URI:?}"          # staging subscriber
: "${MIGRATION_DIR:?}"    # dir with NN_up.sql, NN_down.sql, NN_sub_up.sql, NN_sub_down.sql
: "${LOAD_CMD:=pgbench -c 20 -j 4 -T 120 -N}"   # background write load

log() { printf '[%s] %s\n' "$(date -Iseconds)" "$*"; }

capture_baseline() {
  psql "$PUB_URI" -Atc "SELECT pg_current_wal_lsn()"
  psql "$PUB_URI" -Atc \
    "SELECT COALESCE(sum(confirmed_flush_lsn - '0/0'),0) FROM pg_replication_slots"
}

wait_for_catchup() {
  local pub_lsn deadline=$((SECONDS + 600))
  pub_lsn=$(psql "$PUB_URI" -Atc "SELECT pg_current_wal_lsn()")
  while (( SECONDS < deadline )); do
    local caught
    caught=$(psql "$PUB_URI" -Atc \
      "SELECT bool_and(confirmed_flush_lsn >= '${pub_lsn}'::pg_lsn) FROM pg_replication_slots WHERE slot_type='logical'")
    [[ "$caught" == "t" ]] && { log "subscribers caught up"; return 0; }
    sleep 2
  done
  log "TIMEOUT waiting for subscriber catch-up"; return 1
}

verify_row_parity() {
  # Cheap parity: row counts + a rolling hash on key tables
  for t in "${PARITY_TABLES[@]:-users orders order_items}"; do
    local p s
    p=$(psql "$PUB_URI" -Atc "SELECT count(*), md5(string_agg(md5(t::text), '' ORDER BY 1)) FROM $t t")
    s=$(psql "$SUB_URI" -Atc "SELECT count(*), md5(string_agg(md5(t::text), '' ORDER BY 1)) FROM $t t")
    [[ "$p" == "$s" ]] || { log "PARITY MISMATCH on $t: pub=$p sub=$s"; return 1; }
  done
}

lock_watchdog() {
  # Warn if any relation held ACCESS EXCLUSIVE > 2s during the run
  psql "$PUB_URI" -c "
    SELECT now(), relation::regclass, mode, granted, pid,
           (SELECT query FROM pg_stat_activity WHERE pid = l.pid)
    FROM pg_locks l
    WHERE mode = 'AccessExclusiveLock' AND granted;"
}

# 1. Snapshot state
log "capturing baseline"; capture_baseline

# 2. Start background write load
log "starting load"; $LOAD_CMD "$PUB_URI" >/tmp/load.out 2>&1 &
LOAD_PID=$!
trap 'kill $LOAD_PID 2>/dev/null || true' EXIT

# 3. Apply subscriber-side additive DDL FIRST
for f in "$MIGRATION_DIR"/*_sub_up.sql; do
  log "sub up: $f"; psql "$SUB_URI" -v ON_ERROR_STOP=1 -X -f "$f"
done

# 4. Apply publisher DDL through the safe wrapper (§2.1)
for f in "$MIGRATION_DIR"/*_up.sql; do
  log "pub up: $f"; SQL_FILE="$f" PGURI="$PUB_URI" ./run_ddl.sh
  lock_watchdog
done

# 5. Wait for logical catch-up, verify parity
wait_for_catchup
verify_row_parity

# 6. Test rollback path
for f in $(ls -r "$MIGRATION_DIR"/*_down.sql 2>/dev/null); do
  log "pub down: $f"; SQL_FILE="$f" PGURI="$PUB_URI" ./run_ddl.sh
done
for f in $(ls -r "$MIGRATION_DIR"/*_sub_down.sql 2>/dev/null); do
  log "sub down: $f"; psql "$SUB_URI" -v ON_ERROR_STOP=1 -X -f "$f"
done
wait_for_catchup
verify_row_parity

# 7. Re-apply up (final state) so the staging DB matches what will hit prod
for f in "$MIGRATION_DIR"/*_sub_up.sql; do psql "$SUB_URI" -v ON_ERROR_STOP=1 -X -f "$f"; done
for f in "$MIGRATION_DIR"/*_up.sql;     do SQL_FILE="$f" PGURI="$PUB_URI" ./run_ddl.sh; done
wait_for_catchup
verify_row_parity

log "OK — migration verified end-to-end"
```

### 4.3 Assertions the harness must produce

Green build requires all of:

1. Every `_up.sql` succeeded within the retry budget.
2. No `AccessExclusiveLock` was held longer than 2s (from `lock_watchdog` output).
3. Subscribers caught up within 10 minutes of the last DDL.
4. Row-count + rolling-hash parity on named tables matches publisher and subscriber.
5. `pg_replication_slots.active = true` throughout; `retained_wal < 5 GiB` peak.
6. Rollback (`_down.sql`) applied cleanly and left parity intact.
7. Re-applied `_up.sql` after rollback still leaves parity intact (idempotency).

### 4.4 Extra checks worth wiring in

- **`pg_stat_statements` diff:** capture top queries before/after; alert on plan regressions.
- **Invalid indexes:** fail the build if `pg_index.indisvalid = false` after the run.
- **Trigger fan-out:** run application integration tests against the migrated staging DB before promotion.
- **Restore drill:** every N runs, restore the RDS snapshot into a fresh instance and re-run — proves point-in-time-recovery works with the new schema.

---

## 5. Cleaning Up Deprecated Columns After Production Rollout

Dropping a column looks cheap (`ALTER TABLE ... DROP COLUMN` is metadata-only in PG — no rewrite), but the risk isn't the lock. The risk is that **something is still reading it** — a stale replica, a report, a subscriber, a serialized ORM cache, a Kafka connector snapshotting via logical replication.

Use a four-phase contract, gated on time and evidence.

### 5.1 Phase 0 — Prerequisites (T-∞)

- [ ] The replacement column/table has been in production for ≥ **one full release cycle + one backup retention window** (whichever is longer). Common baseline: 14 days.
- [ ] Application code no longer writes to the deprecated column (verified via code search + logs).
- [ ] Application code no longer reads the deprecated column (verified via `pg_stat_statements`, see §5.2).
- [ ] No logical publication has the column in its `WHERE`/column list.
- [ ] No downstream consumer (analytics warehouse, CDC to Kafka/S3, materialized views) references it.

### 5.2 Phase 1 — Prove no reader exists (T-14d → T-0)

Run this daily for at least 7 days; alert on any hit.

```sql
-- Any statement in pg_stat_statements referencing the column?
SELECT userid::regrole, queryid, calls, rows, query
FROM pg_stat_statements
WHERE query ILIKE '%<column_name>%'
  AND query NOT ILIKE '%information_schema%'
  AND query NOT ILIKE '%pg_catalog%';

-- Any view / matview / function / rule referencing it?
SELECT DISTINCT dependent_ns.nspname || '.' || dependent_view.relname AS referenced_by,
                dependent_view.relkind
FROM pg_depend d
JOIN pg_rewrite r ON r.oid = d.objid
JOIN pg_class dependent_view ON dependent_view.oid = r.ev_class
JOIN pg_namespace dependent_ns ON dependent_ns.oid = dependent_view.relnamespace
JOIN pg_attribute a ON a.attrelid = d.refobjid AND a.attnum = d.refobjsubid
WHERE d.refobjid = '<schema>.<table>'::regclass
  AND a.attname = '<column_name>';

-- Indexes on the column?
SELECT i.relname AS index_name
FROM pg_index x
JOIN pg_class i ON i.oid = x.indexrelid
JOIN pg_attribute a ON a.attrelid = x.indrelid AND a.attnum = ANY(x.indkey)
WHERE x.indrelid = '<schema>.<table>'::regclass
  AND a.attname = '<column_name>';
```

Also check outside the DB: application logs, BI tool query history, dbt model refs (`grep -R "<column_name>"`), Kafka Connect table.include/column configs.

### 5.3 Phase 2 — Tombstone the column (T-0)

Make it invisible to new code without touching data. Reversible in seconds.

```sql
-- 1. Revoke to catch any missed reader (they will 42501 loudly, not silently).
BEGIN;
SET LOCAL lock_timeout = '2s';
REVOKE SELECT (<column_name>) ON <schema>.<table> FROM PUBLIC, <app_role>;
COMMIT;

-- 2. Rename to a tombstoned name so ORM re-syncs surface the removal.
BEGIN;
SET LOCAL lock_timeout = '2s';
ALTER TABLE <schema>.<table>
  RENAME COLUMN <column_name> TO <column_name>_deprecated_YYYYMMDD;
COMMIT;

-- 3. If it was NOT NULL, drop the NOT NULL now — future inserts from stale
--    code paths that omit the column must not fail.
BEGIN;
SET LOCAL lock_timeout = '2s';
ALTER TABLE <schema>.<table>
  ALTER COLUMN <column_name>_deprecated_YYYYMMDD DROP NOT NULL;
COMMIT;
```

Hold this state **at least 7 days**. During this window:

- Monitor error logs for `column ... does not exist` or `permission denied`.
- Confirm every logical subscriber still applies cleanly (any subscriber that referenced the column would error here — that's the point).
- If anything breaks, `RENAME` back — zero data loss.

### 5.4 Phase 3 — Physical drop (T+7d)

Ordering matters under logical replication (destructive change → publisher first):

```sql
-- On PUBLISHER
BEGIN;
SET LOCAL lock_timeout = '2s';
SET LOCAL statement_timeout = '15s';

-- Drop dependent objects explicitly, don't rely on CASCADE surprises.
DROP INDEX CONCURRENTLY IF EXISTS <idx_on_deprecated_col>;  -- run OUTSIDE txn

ALTER TABLE <schema>.<table>
  DROP COLUMN IF EXISTS <column_name>_deprecated_YYYYMMDD;

COMMIT;
```

Then, on each **subscriber**, after confirming the publisher change has replicated as a no-op (logical replication ignores column drops from the publisher — subscriber still has the column):

```sql
-- On each SUBSCRIBER, after publisher drop is confirmed
BEGIN;
SET LOCAL lock_timeout = '2s';
ALTER TABLE <schema>.<table>
  DROP COLUMN IF EXISTS <column_name>_deprecated_YYYYMMDD;
COMMIT;

-- Refresh subscription metadata so column list matches
ALTER SUBSCRIPTION <sub_name> REFRESH PUBLICATION;
```

**Important RDS notes:**

- `ALTER TABLE ... DROP COLUMN` is O(1) — no rewrite. The column is soft-deleted in `pg_attribute` (marked `attisdropped`), and disk space is reclaimed only when rows are rewritten by later `UPDATE`s or a `VACUUM FULL` / `pg_repack`. Do **not** immediately `VACUUM FULL` a large table to reclaim space — schedule `pg_repack` in a maintenance window if the column was wide.
- If the column had a `DEFAULT`, the default expression's dependency is removed automatically.
- If it participated in a UNIQUE or FK, drop those constraints first (each in its own transaction under the safe wrapper).

### 5.5 Phase 4 — Post-drop verification (T+7d + 1h)

```sql
-- Column is truly gone
SELECT attname, attisdropped
FROM pg_attribute
WHERE attrelid = '<schema>.<table>'::regclass
  AND attname LIKE '%deprecated%';

-- No invalid indexes left behind
SELECT c.relname FROM pg_index i
JOIN pg_class c ON c.oid = i.indexrelid WHERE NOT i.indisvalid;

-- Replication healthy
SELECT slot_name, active, confirmed_flush_lsn,
       pg_wal_lsn_diff(pg_current_wal_lsn(), confirmed_flush_lsn) AS lag_bytes
FROM pg_replication_slots WHERE slot_type = 'logical';

-- Subscriber apply worker alive
SELECT subname, pid, received_lsn, latest_end_lsn, latest_end_time
FROM pg_stat_subscription;
```

Then:

- Take a fresh RDS snapshot (labelled `post-drop-<column>-<date>`) so the pre-drop snapshot can eventually age out.
- Update the schema catalog / data dictionary.
- Close the change ticket with links to the monitoring dashboards captured during the tombstone window.

---

## Appendix A — Standing parameter group settings (RDS)

| Parameter | Value | Reason |
|---|---|---|
| `rds.logical_replication` | `1` | Enables `wal_level = logical` and `max_replication_slots`/`max_wal_senders` |
| `lock_timeout` (session-level in migrations) | `2s` | Fail-fast on lock contention |
| `statement_timeout` (session-level) | `15s` for DDL, per-app for OLTP | Bounded blast radius |
| `idle_in_transaction_session_timeout` | `60s` globally, `5s` in migrations | Prevents catalog xmin bloat + lock queue jams |
| `log_lock_waits` | `on` | Post-hoc diagnosis |
| `deadlock_timeout` | `1s` | Default; keep |
| `max_slot_wal_keep_size` | Sized to ≤ 30% of storage | Prevents runaway slots from filling disk |
| `wal_sender_timeout` | `60s` (raise carefully during heavy backfills) | Survives short apply spikes |

## Appendix B — Rollback quick-reference

| Failure | Immediate action |
|---|---|
| DDL retries exhausted | Abort deploy; check `pg_stat_activity` for blocker; keep app on N-1 schema-compatible build. |
| Invalid index after `CONCURRENTLY` | `DROP INDEX CONCURRENTLY` the invalid one; re-run creation. |
| Subscriber apply worker errored on schema mismatch | Apply missing DDL on subscriber; `ALTER SUBSCRIPTION ... ENABLE;` — do not drop the slot. |
| Replication slot inactive & growing | Identify why (subscriber down, network); revive subscriber. Dropping the slot forces full resync. |
| Disk pressure from WAL retention | Scale storage (online, rate-limited); pause backfill; do **not** drop the slot unless data loss is acceptable. |
| Column tombstone caused reader failure | `ALTER TABLE ... RENAME COLUMN ... TO <original>` and `GRANT SELECT` back. |

## Appendix C — Sources

- Postgres.ai — [Zero-downtime Postgres schema migrations need lock_timeout and retries](https://postgres.ai/blog/20210923-zero-downtime-postgres-schema-migrations-lock-timeout-and-retries)
- Hubert Lubaczewski (depesz) — [How to run short ALTER TABLE without long locking concurrent queries](https://www.depesz.com/2019/09/26/how-to-run-short-alter-table-without-long-locking-concurrent-queries/)
- PostgreSQL docs — [Logical Replication Restrictions](https://www.postgresql.org/docs/current/logical-replication-restrictions.html), [`ALTER TABLE`](https://www.postgresql.org/docs/current/sql-altertable.html), [`CREATE INDEX`](https://www.postgresql.org/docs/current/sql-createindex.html)
- AWS — [Using PostgreSQL logical replication with Amazon RDS](https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/CHAP_PostgreSQL.html#PostgreSQL.Concepts.General.FeatureSupport.LogicalReplication), [PostgreSQL logical replication: how to replicate only the data that you need](https://aws.amazon.com/blogs/database/postgresql-logical-replication-how-to-replicate-only-the-data-that-you-need/)
- OneUptime — [How to Monitor PostgreSQL Replication Lag](https://oneuptime.com/blog/post/2026-01-21-postgresql-replication-lag-monitoring/view)
- DEV Community — [`CREATE INDEX CONCURRENTLY`: The Complete PostgreSQL Guide](https://dev.to/mickelsamuel/create-index-concurrently-the-complete-postgresql-guide-b7m)
