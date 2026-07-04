# Zero-Downtime Database Migration Playbook

**Stack assumption:** PostgreSQL primary + read replicas, containerized microservices behind a load balancer, ORM-based migrations (Alembic / ActiveRecord / Prisma / Flyway / Liquibase). Substitute your tool where noted.

**Core principle:** The database schema and every deployed service version must remain compatible with the *previous* and *next* schema state at all times. Never ship a migration that requires a specific code version to be running everywhere at once.

---

## 1. The Expand → Migrate → Contract Pattern

A single logical schema change is split into a sequence of independently deployable, individually reversible steps. No step blocks production traffic; no step depends on a coordinated cutover.

```
   Expand           Migrate            Contract
 ┌────────┐      ┌──────────┐       ┌──────────┐
 │ Add    │ ───▶ │ Dual     │  ───▶ │ Drop     │
 │ new    │      │ write /  │       │ old      │
 │ shape  │      │ dual read│       │ shape    │
 └────────┘      └──────────┘       └──────────┘
   Additive        Backfill +         Destructive
   only            verify             (only after
                                       full soak)
```

### Phase 0 — Design & Impact Review

Before any DDL runs:

- [ ] Write a **Migration Design Doc** (one page): current shape, target shape, why, blast radius, rollback plan.
- [ ] Identify every service, cron job, analytics pipeline, and replica that reads/writes the affected table(s). Use `pg_stat_user_tables` + service ownership map.
- [ ] Classify the change against the **Backward-Compatibility Audit Checklist** (Section 3). If any red-flag item is triggered, expand it into multiple steps.
- [ ] Estimate row count and lock behavior. For tables >10M rows, plan a batched backfill and confirm no `ACCESS EXCLUSIVE` locks in the plan.
- [ ] Confirm replica lag baseline (`pg_stat_replication.replay_lag`) and set a lag budget (e.g., <5s) that pauses backfill above threshold.

### Phase 1 — Expand (Additive DDL)

Goal: introduce the new shape without breaking any currently deployed reader or writer.

**Rules:**
- New columns MUST be nullable OR have a constant default (Postgres 11+ stores constant defaults in metadata — no table rewrite).
- New tables and indexes are always safe to add.
- All indexes on hot tables use `CREATE INDEX CONCURRENTLY`.
- All constraints added as `NOT VALID` first, then `VALIDATE CONSTRAINT` in a later step.

**Postgres examples:**

```sql
-- ✅ Safe: nullable column, no rewrite
ALTER TABLE orders ADD COLUMN customer_uuid uuid NULL;

-- ✅ Safe on PG 11+: constant default is metadata-only
ALTER TABLE orders ADD COLUMN status_v2 text NOT NULL DEFAULT 'pending';

-- ✅ Safe: concurrent index build, no write lock
CREATE INDEX CONCURRENTLY idx_orders_customer_uuid
  ON orders (customer_uuid);

-- ✅ Safe: add FK without validating existing rows
ALTER TABLE orders
  ADD CONSTRAINT fk_orders_customer_uuid
  FOREIGN KEY (customer_uuid) REFERENCES customers(uuid)
  NOT VALID;
```

**Deploy gate:** ship the DDL alone. No application code depends on the new column yet. Wait for the migration to be applied to all replicas before moving on.

### Phase 2 — Dual-Write

Goal: every write path populates both the old and new shape. Reads still come from the old shape.

**Application changes (per service):**

```python
# Example: adding customer_uuid alongside legacy customer_id
def create_order(payload):
    order = Order(
        customer_id=payload.customer_id,
        customer_uuid=lookup_uuid(payload.customer_id),  # NEW
        ...
    )
    session.add(order)
```

**Rules:**
- Dual-write is guarded by a feature flag (`dual_write_customer_uuid`) so it can be disabled instantly without redeploy.
- Writes to the new column MUST NOT fail the transaction if the value is unavailable — log a warning, insert NULL, and let the backfill catch it.
- Deploy dual-write to **every** service that writes the table. Track per-service rollout in the state template (Section 4).

**Deploy gate:** dual-write is at 100% on all writer services for at least one full business cycle (typically 24h) before backfill.

### Phase 3 — Backfill

Goal: populate the new shape for historical rows the dual-write didn't touch.

**Batched backfill pattern:**

```sql
-- Run repeatedly until zero rows updated
WITH batch AS (
  SELECT id FROM orders
  WHERE customer_uuid IS NULL
    AND customer_id IS NOT NULL
  ORDER BY id
  LIMIT 5000
  FOR UPDATE SKIP LOCKED
)
UPDATE orders o
SET customer_uuid = c.uuid
FROM batch b
JOIN customers c ON c.legacy_id = o.customer_id
WHERE o.id = b.id;
```

**Rules:**
- Batch size tuned so a single batch commits in <200ms.
- Sleep 50–200ms between batches on high-throughput tables.
- Backfill worker monitors `pg_stat_replication` and pauses if replica lag exceeds budget.
- Idempotent — safe to restart, safe to run twice.
- For very large tables (>500M rows), consider `pg_repack` or logical replication into a shadow table.

### Phase 4 — Verify (Data Sync)

Goal: prove old and new shapes agree before any reader is switched.

**Automated checks (run continuously during dual-write window):**

```sql
-- Drift check: rows where new shape disagrees with derived value from old
SELECT count(*) AS drift_rows
FROM orders o
JOIN customers c ON c.legacy_id = o.customer_id
WHERE o.customer_uuid IS DISTINCT FROM c.uuid;
-- Expect: 0

-- Coverage check: rows still missing new shape
SELECT count(*) AS missing_new
FROM orders
WHERE customer_id IS NOT NULL AND customer_uuid IS NULL;
-- Expect: 0

-- Row-count parity across regions/replicas
SELECT count(*) FROM orders;  -- Compare across primary + each replica
```

**Sign-off criteria:**
- [ ] Drift count = 0 for 3 consecutive checks over ≥24h.
- [ ] Coverage = 100% for non-NULL source rows.
- [ ] Row counts match across all replicas.
- [ ] Sampled 1000 rows manually spot-checked.
- [ ] Downstream analytics/ETL confirms parity in their extracts.

### Phase 5 — Dual-Read (Shadow → Cutover)

Goal: switch readers to the new shape safely.

**Sub-phases:**

1. **Shadow read** — services read *both* shapes, compare in-process, log divergence, but still serve the old value. Run for ≥24h.
2. **Flag flip** — flip `read_from_uuid` flag to `true` per service, watching error rates and divergence logs. Roll one service at a time.
3. **Old read removed** — after all services are at 100% new-read for ≥1 week, delete the old-read code path.

**Rules:**
- Shadow-read divergence >0.01% pauses the rollout and re-opens Phase 4.
- Each service's cutover is independent — the state tracker (Section 4) is the source of truth for who has moved.

### Phase 6 — Contract (Destructive Cleanup)

Goal: remove the old shape once nothing references it.

**Prerequisites — all must be true:**

- [ ] Every service is at 100% new-read for ≥7 days.
- [ ] Dual-write to the old column has been disabled and redeployed for ≥7 days (writes go to new shape only).
- [ ] Grep the entire monorepo + every service repo for the old column name → zero hits.
- [ ] Query `pg_stat_user_tables` / `pg_stat_statements` on the primary for ≥48h → zero references to the old column.
- [ ] Analytics/warehouse pipelines confirmed migrated.

**Destructive operations:**

```sql
-- Now safe: no readers, no writers
ALTER TABLE orders DROP COLUMN customer_id;

-- Now safe to tighten: backfill guarantees no NULLs remain
-- Use NOT VALID + VALIDATE to avoid full table lock
ALTER TABLE orders
  ALTER COLUMN customer_uuid SET NOT NULL;
-- If the table is huge, prefer:
--   ALTER TABLE orders ADD CONSTRAINT customer_uuid_not_null
--     CHECK (customer_uuid IS NOT NULL) NOT VALID;
--   ALTER TABLE orders VALIDATE CONSTRAINT customer_uuid_not_null;
--   (Then drop the check and set the column NOT NULL — cheap after validation.)

-- Validate the FK added earlier as NOT VALID
ALTER TABLE orders VALIDATE CONSTRAINT fk_orders_customer_uuid;
```

**Rollback:** contract steps are the only irreversible ones. Take a pre-contract snapshot / point-in-time-recovery marker.

---

## 2. Rollback Playbook by Phase

| Phase | Rollback action | Downtime? |
|---|---|---|
| Expand | `DROP COLUMN` / `DROP INDEX CONCURRENTLY` | None |
| Dual-write | Flip feature flag off | None |
| Backfill | Stop worker; new-shape data can stay | None |
| Verify | No action needed | None |
| Dual-read | Flip read flag back to old | None |
| Contract | Restore from PITR / snapshot | **Yes** — this is why contract requires soak time |

---

## 3. Backward-Compatibility Audit Checklist

Run this checklist against every proposed migration. **Any 🔴 requires expansion into a multi-step plan.** Any 🟡 requires explicit review sign-off.

### 🔴 Never do directly in production

- [ ] **Rename a column** — always add new, dual-write, migrate readers, drop old.
- [ ] **Rename a table** — same pattern (add view aliasing if needed).
- [ ] **Change a column type in a narrowing way** (e.g., `text` → `varchar(50)`, `bigint` → `int`).
- [ ] **Add `NOT NULL` without a default on an existing column** — causes table rewrite + rejects in-flight writes from old code.
- [ ] **Add a `CHECK` or `FOREIGN KEY` constraint without `NOT VALID`** — takes `ACCESS EXCLUSIVE` lock while validating.
- [ ] **Add a `UNIQUE` constraint without `CREATE UNIQUE INDEX CONCURRENTLY`** first.
- [ ] **`CREATE INDEX` without `CONCURRENTLY`** on a table with writes.
- [ ] **Drop a column still referenced by deployed code.**
- [ ] **Change the primary key** without a shadow-table migration.
- [ ] **Reorder or remove enum values** (`ALTER TYPE ... DROP VALUE` isn't supported; renaming breaks readers).
- [ ] **Change default value AND rely on existing rows adopting it** — defaults only apply to new rows.
- [ ] **`ALTER COLUMN ... SET DATA TYPE`** requiring a scan (any non-binary-coercible cast).
- [ ] **Combine multiple `ALTER TABLE` clauses that each take a lock** in one long transaction.

### 🟡 Requires review + lock-timeout guardrails

- [ ] Adding a column with a **non-constant** default (e.g., `DEFAULT gen_random_uuid()`) — rewrites the table on <PG 11 and on some managed variants.
- [ ] Adding a `NOT NULL` column with a constant default on a very large table — usually safe on PG 11+, verify on your version.
- [ ] Dropping an index that might be in use — check `pg_stat_user_indexes.idx_scan`.
- [ ] `VACUUM FULL`, `CLUSTER`, `REINDEX` (non-concurrent) — all take `ACCESS EXCLUSIVE`.
- [ ] Any migration on a partitioned table's parent — verify lock cascades to partitions.
- [ ] Triggers or generated columns added on hot tables — measure write overhead.

### 🟢 Generally safe (still use `lock_timeout` and `statement_timeout`)

- [ ] Adding a nullable column.
- [ ] Adding a column with a constant default (PG 11+).
- [ ] `CREATE INDEX CONCURRENTLY`.
- [ ] Adding constraints as `NOT VALID`, then `VALIDATE` separately.
- [ ] Creating new tables.
- [ ] Adding new enum values with `ALTER TYPE ... ADD VALUE` (append only).

### Guardrails to set on every migration session

```sql
SET lock_timeout       = '2s';    -- Fail fast rather than block writers
SET statement_timeout  = '5min';  -- Bound worst-case
SET idle_in_transaction_session_timeout = '10s';
```

---

## 4. Multi-Service / Multi-Replica State Tracker

One row per migration. Track every service and replica independently — a migration is "done" only when every row is green.

### Migration record

| Field | Example | Notes |
|---|---|---|
| `migration_id` | `2026-07-01-orders-customer-uuid` | Date + slug, matches the migration file name |
| `title` | Replace `orders.customer_id` with `customer_uuid` | Human-readable |
| `owner` | @you | Single DRI |
| `design_doc` | Notion link | Required before Phase 1 |
| `current_phase` | Expand / Dual-write / Backfill / Verify / Dual-read / Contract / Done | See Section 1 |
| `phase_entered_at` | 2026-07-01T14:00Z | For soak-time tracking |
| `soak_minimum` | 24h / 7d per phase | Blocks advancement |
| `rollback_plan` | Free text + PITR marker | Required before Phase 6 |
| `blast_radius` | Table + row count + QPS | From `pg_stat_user_tables` |
| `linked_pr_expand` | github.com/... | |
| `linked_pr_dual_write` | github.com/... | |
| `linked_pr_dual_read` | github.com/... | |
| `linked_pr_contract` | github.com/... | |

### Per-service rollout matrix

Every writer/reader gets its own row. Advance the migration only when the min across all services meets the phase requirement.

| Service | Repo | Role (R/W/RW) | Expand deploy | Dual-write % | Shadow-read % | New-read % | Old code removed | Owner |
|---|---|---|---|---|---|---|---|---|
| checkout-api | acme/checkout | RW | ✅ 2026-07-01 | 100% | 100% | 100% | ✅ | @alice |
| fulfillment-worker | acme/fulfillment | RW | ✅ 2026-07-01 | 100% | 100% | 50% | ⏳ | @bob |
| reporting-etl | acme/etl | R | ✅ 2026-07-01 | n/a | 100% | 0% | ⏳ | @carol |
| mobile-bff | acme/bff | R | ✅ 2026-07-01 | n/a | 100% | 100% | ✅ | @dan |
| legacy-cron | acme/cron | W | ⏳ | 0% | n/a | n/a | ❌ | @eve |

### Per-replica DDL status

| DB / Replica | Region | DDL applied | Replay lag at apply | Backfill progress | Verify signoff |
|---|---|---|---|---|---|
| primary | us-east-1 | ✅ 14:02Z | — | 100% | ✅ |
| replica-1 | us-east-1 | ✅ 14:02Z | 0.4s | 100% | ✅ |
| replica-2 | us-west-2 | ✅ 14:03Z | 1.2s | 100% | ✅ |
| replica-eu | eu-west-1 | ✅ 14:05Z | 3.8s | 100% | ⏳ |
| analytics-follower | us-east-1 | ✅ 14:10Z | 8s | 92% | ❌ |

### Phase advancement rules (encode as CI/bot check)

- **Expand → Dual-write:** all rows in per-replica table show `DDL applied ✅`.
- **Dual-write → Backfill:** all writer services at `Dual-write 100%` for ≥24h.
- **Backfill → Verify:** coverage = 100%, no active backfill job.
- **Verify → Dual-read:** drift = 0 for 3 consecutive checks over ≥24h.
- **Dual-read → Contract:** all reader services at `New-read 100%` for ≥7d AND all `Old code removed ✅`.
- **Contract → Done:** destructive DDL applied to every replica; snapshot/PITR marker recorded.

---

## 5. Templates for Common Change Types

| Change | Steps |
|---|---|
| **Rename column** `a` → `b` | Add `b` nullable → dual-write → backfill → verify → shadow read → cutover → drop `a` |
| **Change column type** `int` → `bigint` | Add `b_new bigint` → dual-write → backfill → verify → cutover reads → drop `a` → rename `b_new` → `a` (single-service brief pause acceptable, or keep the new name) |
| **Split column** `full_name` → `first_name`, `last_name` | Add both nullable → dual-write derived values → backfill → verify → cutover reads → drop `full_name` |
| **Merge columns** | Add unified column → dual-write concat → backfill → verify → cutover → drop originals |
| **Add NOT NULL to existing column** | Backfill NULLs to a sentinel → add `CHECK (col IS NOT NULL) NOT VALID` → `VALIDATE CONSTRAINT` → `SET NOT NULL` → drop check |
| **Rename table** | Create new table → dual-write via trigger or app → backfill → cutover reads → drop old (or keep as updatable view for grace period) |
| **Change PK** | Shadow table with new PK → logical replication or app dual-write → cutover → rename |
| **Drop table** | Rename to `_deprecated_YYYYMMDD` first, wait 30d, then drop |

---

## 6. Operational Guardrails

- **Migrations run as a dedicated low-privilege role**, not the application user.
- **CI check** parses every migration file and fails on `NOT VALID`-missing constraints, `CREATE INDEX` without `CONCURRENTLY`, `DROP COLUMN`, `ALTER COLUMN ... TYPE`, `NOT NULL` without default, table renames.
- **`lock_timeout` and `statement_timeout`** set in the migration runner, not relied on from `postgresql.conf`.
- **Migration runner is idempotent** and records each step in a `schema_migrations` table with `started_at`, `finished_at`, `applied_by`, `checksum`.
- **PgBouncer / connection poolers** flushed of prepared statements after DDL that changes column shape.
- **Observability:** dashboards for replica lag, long-running queries, lock waits, and per-column write rate (via `pg_stat_statements`) surfaced during every migration window.
- **On-call awareness:** every Phase 1 and Phase 6 change posts to `#db-migrations` before it runs.

---

## 7. Migration Design Doc Template (copy per change)

```markdown
# Migration: <slug>

**Migration ID:** YYYY-MM-DD-<slug>
**Owner:** @handle
**Status:** Draft | Expand | Dual-write | Backfill | Verify | Dual-read | Contract | Done

## Current shape
<DDL snippet>

## Target shape
<DDL snippet>

## Why
1–3 sentences.

## Blast radius
- Table: `orders` (~120M rows, ~2k writes/s peak)
- Services touching this table: checkout-api, fulfillment-worker, reporting-etl, mobile-bff, legacy-cron
- Downstream: Snowflake `raw.orders` extract, Looker model `orders_v2`

## Audit checklist result
- 🔴 items triggered: <list, or "none">
- 🟡 items triggered: <list>
- Mitigation: <how the plan avoids each>

## Step-by-step plan
1. Expand: <DDL>
2. Dual-write: <PR link>
3. Backfill: <script link, batch size, ETA>
4. Verify: <query list, SLO>
5. Dual-read: <PR link, rollout plan>
6. Contract: <DDL, prerequisites>

## Rollback
- Per phase: see playbook §2
- Contract PITR marker: <to be filled at Phase 6>

## Sign-offs
- [ ] DB owner
- [ ] Each service owner
- [ ] SRE on-call
- [ ] Analytics/data platform
```
