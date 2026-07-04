# CI Coverage Analysis vs. Zero-Downtime DDL Playbook

**Status of this document:** I do not have direct access to your CI repository in this session, so this is a **self-audit rubric** rather than a review of specific files. Run each rubric item against your actual pipeline — the "How to verify" column is a grep or a single command you can run in your repo. Score each row as ✅ / ⚠️ / ❌ and the aggregate tells you the honest gap.

---

## Rubric — coverage of the 7 green-build assertions

| # | Playbook assertion | What "covered" looks like | How to verify in your repo | Common failure mode |
|---|---|---|---|---|
| 1 | Every `_up.sql` succeeds within the retry budget | CI invokes a `run_ddl.sh`-style wrapper that sets `lock_timeout` + retries, not a bare `psql -f` | `grep -RIn "lock_timeout\|statement_timeout" ci/ .github/ scripts/ db/` — you should see `SET LOCAL lock_timeout` in the runner **and** a retry loop with exponential backoff | Migrations run via `psql -f` or an ORM CLI (Flyway/Liquibase/Alembic) with no `lock_timeout` and no retry logic — first prod lock contention takes the DB down |
| 2 | No `AccessExclusiveLock` held longer than 2s | A background poller samples `pg_locks` during the migration and fails the build if it observes `AccessExclusiveLock granted = true` for more than the threshold | `grep -RIn "pg_locks\|AccessExclusive" ci/ .github/ tests/` | No lock observation at all — the build "passes" even when a migration would freeze prod under load |
| 3 | Subscribers catch up within 10 min of last DDL | A `wait_for_catchup` function polls `pg_replication_slots.confirmed_flush_lsn >= <captured_pub_lsn>` with a timeout | `grep -RIn "confirmed_flush_lsn\|pg_replication_slots\|pg_current_wal_lsn" ci/ .github/` | No logical replication in CI at all — CI runs against a single Postgres container, so replication bugs are only discovered in staging or prod |
| 4 | Row-count + rolling-hash parity on named tables | For each table in a parity list, run `SELECT count(*), md5(string_agg(md5(t::text), '' ORDER BY <pk>))` on publisher and subscriber, then compare | `grep -RIn "md5(string_agg\|count(\*)" ci/` and check for both publisher and subscriber URIs | Parity is checked implicitly (integration tests pass after migration) but no explicit publisher-vs-subscriber row/hash equality — silent replication drift can pass CI |
| 5 | `pg_replication_slots.active = true` throughout; `retained_wal < 5 GiB` peak | Background monitor logs slot state each interval; build fails if `active=false` or peak `retained_wal > threshold` | `grep -RIn "retained_wal\|pg_wal_lsn_diff\|slot_type = 'logical'" ci/` | Slot health never checked — a slot going inactive during a large backfill is only detected when publisher disk fills |
| 6 | Rollback (`_down.sql`) applied cleanly and left parity intact | Harness runs `_down.sql` in reverse order after `_up.sql`, re-checks parity, then re-applies `_up.sql` | `grep -RIn "_down\.sql\|rollback" ci/ db/migrations/` | Down migrations either don't exist or are never exercised in CI; rollback is discovered to be broken during an incident |
| 7 | Re-applied `_up.sql` after rollback still leaves parity intact (idempotency) | Same as (6) but the `_up.sql` is guarded by `IF NOT EXISTS` / `DO $$ ... $$` blocks and CI applies it a second time | `grep -RIn "IF NOT EXISTS\|CREATE OR REPLACE\|DO \\\$" db/migrations/` and confirm CI re-applies | Migrations use bare `ADD COLUMN` etc. — a retried deploy after a partial failure crashes on "column already exists" |

**Scoring guide.** Any ❌ on rows 1, 2, 3, or 5 is a production incident waiting to happen. ❌ on 4, 6, or 7 is a silent-correctness bug waiting to happen.

---

## The two gaps you flagged, in detail

### Gap A — No background load during migration tests

**Why it's a real gap, not a nice-to-have.** DDL that looks instant on an idle table can take an `AccessExclusiveLock` behind a long-running `SELECT` and stall for minutes under real traffic. A CI job that runs `ALTER TABLE` against a quiescent database will pass every time and teach you nothing about production behavior. The playbook's assertion 2 (no `AccessExclusiveLock` > 2s) is meaningless without concurrent writers/readers competing for that lock.

**How to tell if your CI has this gap:**

```bash
# In the repo root:
grep -RIn "pgbench\|pg_bench\|load\|concurrent\|background" ci/ .github/ tests/ | \
  grep -vE "package-lock|yarn\.lock|node_modules"
```

If nothing meaningful returns, or hits only reference application load tests (not database-level concurrent writes during the migration itself), you have this gap.

**What "fixed" looks like:** a `pgbench` (or custom writer) started as a **background process** before the migration runs, generating ≥ 10 concurrent client sessions of mixed reads/writes against tables touched by the migration, killed on exit. See `ci/migration_tests/lib/load.sh` in the PR.

### Gap B — No row-count / parity verification against a logical subscriber

**Why it's a real gap.** Logical replication silently diverges when: (a) DDL is applied out of order (subscriber before publisher or vice versa), (b) a column filter or row filter on the publication drops rows unexpectedly, (c) a trigger on the subscriber alters incoming data, (d) `REPLICA IDENTITY` is wrong and updates don't replicate. None of these fail loudly — the apply worker just quietly ships fewer/different rows.

**How to tell if your CI has this gap:**

```bash
# Look for parity verification between two Postgres instances:
grep -RIn "SUB_URI\|subscriber\|logical_replication\|pg_stat_subscription" ci/ .github/
```

If your CI has only one Postgres service, you cannot possibly be verifying parity — you have the gap by construction.

**What "fixed" looks like:** CI spins up **two** Postgres services (publisher + subscriber), configures a `PUBLICATION`/`SUBSCRIPTION` between them, and after each migration step compares `count(*)` and a stable rolling MD5 for every table on a `PARITY_TABLES` list. See `ci/migration_tests/lib/parity.sh` in the PR.

---

## Other high-frequency misses I'd expect to find

Beyond the two you flagged, these are the gaps I see most often in real repos that already have migration CI. Grep your codebase for each:

1. **No detection of invalid indexes** after `CREATE INDEX CONCURRENTLY`. `grep -RIn "indisvalid" ci/` — should be there, usually isn't.
2. **No `idle_in_transaction_session_timeout`** in the migration session. Long-lived transactions from a stuck migration step bloat `catalog_xmin` and inflate `pg_catalog`.
3. **No test that the `_down.sql` actually reverts** — many teams write `_down.sql` files and never execute them.
4. **No PG-version matrix.** RDS parameter defaults and DDL semantics changed meaningfully at 11, 12, 13, 14, 15, 16. If CI only tests one version, upgrades are unverified.
5. **No `pg_stat_statements` diff** across the migration — plan regressions caused by new indexes or new columns aren't caught.
6. **Deprecated-column tombstone period not enforced by CI.** Nothing prevents a PR from adding a `DROP COLUMN` migration on the same day the column stopped being read.

The PR below addresses 1, 2, 3, 4 directly. 5 and 6 are follow-ups.
