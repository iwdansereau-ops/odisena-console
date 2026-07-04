# ci: add logical-replication migration test harness

## Why

Our current CI applies migrations against a single Postgres container with no concurrent load and no downstream subscriber. That configuration cannot exercise the two failure modes that most often bite us in staging/prod:

1. **Lock contention under real traffic.** DDL that runs instantly on an idle DB can block for minutes behind a long-running `SELECT` under production load. Our CI has no way to observe this today.
2. **Logical replication drift.** Schema changes applied out of order between publisher and subscriber cause the apply worker to fail silently, or to ship diverged data. We currently detect this only when a downstream service breaks.

This PR closes both gaps and hardens the pipeline against all seven green-build assertions defined in the [Zero-Downtime DDL Playbook](../docs/zero_downtime_ddl_playbook.md).

## What this PR does

- Adds `.github/workflows/migration-tests.yml` — a matrix job (PG 14/15/16) that spins up **two** Postgres containers wired as publisher + logical subscriber.
- Adds `ci/migration_tests/run.sh` — orchestrates the harness end-to-end.
- Adds `ci/migration_tests/lib/*.sh` — small, testable library modules:
  - `ddl.sh` — safe wrapper (`lock_timeout` + retry with exponential backoff), matches the playbook's `run_ddl.sh`.
  - `load.sh` — background `pgbench` writer generating concurrent read/write traffic during the migration.
  - `parity.sh` — row-count + rolling MD5 hash comparison between publisher and subscriber.
  - `replication.sh` — replication health check, `wait_for_catchup`, and slot monitor.
  - `locks.sh` — 200ms-cadence `pg_locks` sampler; fails the build if any `AccessExclusiveLock` is held longer than 2s.
  - `plan_regression.sh` + `plan_diff.py` + `pgss_diff.py` + `plan_registry.py` — captures `EXPLAIN (FORMAT JSON)` for a registry of key queries pre/post migration and fails the build on plan regressions (cost, seq-scan appearance, index drop, join-method downgrade). See `docs/plan_regression.md`.
- Adds `ci/migration_tests/key_queries.yml` — starter registry of hot queries to gate on.
- Adds `ci/migration_tests/lib/pgss_topn.py` — one-shot generator that turns `pg_stat_statements` into a real `key_queries.yml` skeleton for the specific workload.
- Adds `ci/migration_tests/lib/notion_report.sh` — posts a row to the Notion 📋 DDL Change Log at end of every successful production run (see `docs/notion_integration.md`).
- Adds `ci/migration_tests/lib/correlate_alerts.py` — runs on a 5-min cron; auto-links alerts to the in-flight DDL row via the two-way relation.
- Adds `ci/migration_tests/lib/cleanup_sla.py` — runs Monday mornings; fails the run and posts a Slack digest if any DDL row is stuck in P0/P1/P2 beyond the SLA (3/7/14 days).
- Adds `.github/workflows/cleanup-sla.yml` and `.github/workflows/alert-correlation.yml` — the two crons above.
- Adds `docs/coverage_analysis.md` — the rubric the harness is built against.
- Adds `docs/notion_integration.md` — wiring guide for the three Notion touch-points.
- Adds `ci/stress_test/` — five-phase 2 TB staging stress-test harness (`stress_test.sh` orchestrator, pgbench workloads for steady + 50 % spike, backfill loop, monitor sampler, abort-trigger enforcer, and `analyze.py` verdict engine). Proves the backfill runbook's pause/resume triggers actually fire when replay lag crosses 500 MB. See `docs/stress_test.md`.
- Adds `.github/workflows/stress-test.yml` — quarterly + on-demand runner (self-hosted, targets staging).
- Adds `ci/scheduled_regression/` — **Monday-morning plan-regression suite** (`scheduled_regression.sh` orchestrator, `baseline_manager.py` rolling 30-day S3-or-local registry, `baseline_aware_analyzer.py` comparing each query to its 30-day median with cost/node/plan-shape/index-lost/seq-scan-appeared signals, and `slack_digest.py` posting a formatted digest with best-effort Notion DDL Change Log correlation). See `docs/scheduled_regression.md`.
- Adds `.github/workflows/weekly-plan-regression.yml` — Monday 09:00 ET cron (`0 13 * * 1`) + `workflow_dispatch` with `compare`/`refresh`/`dry-run` modes.

## Assertion coverage after this PR

| # | Playbook assertion | Enforced by |
|---|---|---|
| 1 | Every `_up.sql` succeeds within retry budget | `ddl::apply_with_retry` |
| 2 | No `AccessExclusiveLock` held > `LOCK_MAX_MS` (default 2000ms) | `locks::start_watchdog` + `locks::assert_no_long_hold` |
| 3 | Subscribers caught up within `CATCHUP_TIMEOUT` (default 600s) | `replication::wait_for_catchup` |
| 4 | Row-count + rolling-hash parity | `parity::verify` on `PARITY_TABLES` |
| 5 | Slot active throughout; `retained_wal < SLOT_WAL_MAX_MB` peak | `replication::start_monitor` + `replication::assert_slot_healthy` |
| 6 | Rollback (`_down.sql`) clean, parity intact | Down-apply block + second `parity::verify` |
| 7 | Re-applied `_up.sql` idempotent, parity intact | Re-apply block + third `parity::verify` |

Bonus: `ddl::assert_no_invalid_indexes` catches leftover `INVALID` indexes from failed `CREATE INDEX CONCURRENTLY`.

**Plan-regression gate:** captures `EXPLAIN (FORMAT JSON)` for the queries in `ci/migration_tests/key_queries.yml` before and after the migration, and fails the build if any query shows both a cost regression > 10% and a structural signal (new `Seq Scan`, dropped index in the plan, join downgrade). Strict mode (`PLAN_STRICT=1`) matches the literal "cost OR nodes > 10%" gate. `pg_stat_statements` is captured for reporting; enforcement is opt-in via `PGSS_ENFORCE=1`. Full design in [`docs/plan_regression.md`](docs/plan_regression.md).

## What this PR does *not* do (deliberate scope cuts)

- Does not replace the existing migration runner (Flyway / Alembic / dbmate / etc.). The harness invokes raw SQL files — bootstrapping still happens via `db/schema.sql` or your existing tool. Swap the "Seed baseline schema" step for your standard bootstrap command.
- Does not enforce a tombstone period for `DROP COLUMN` migrations at PR time — that's a repo policy check, not a runtime test. Follow-up.
- Plan-regression module does not yet detect `Sort`-method changes, `HashAggregate` → `GroupAggregate` downgrades, or partition-pruning regressions. Straightforward to add — see `plan_diff.py::compare`.

## How to try it locally

```bash
# Two Postgres 16 containers with logical replication
docker run -d --name pub -e POSTGRES_PASSWORD=postgres -e POSTGRES_DB=appdb \
  -p 5433:5432 postgres:16 -c wal_level=logical -c max_replication_slots=10 -c max_wal_senders=10
docker run -d --name sub -e POSTGRES_PASSWORD=postgres -e POSTGRES_DB=appdb \
  -p 5434:5432 postgres:16 -c max_replication_slots=10

# ... apply schema, create publication + subscription (see workflow) ...

PUB_URI=postgres://postgres:postgres@127.0.0.1:5433/appdb \
SUB_URI=postgres://postgres:postgres@127.0.0.1:5434/appdb \
MIGRATION_DIR=db/migrations/pending \
ci/migration_tests/run.sh
```

## Migration file conventions this harness expects

Files under `MIGRATION_DIR` are picked up by suffix:

- `NN_up.sql`         — publisher-side additive DDL (applied via safe wrapper)
- `NN_down.sql`       — publisher-side rollback
- `NN_sub_up.sql`     — subscriber-side matching DDL (applied directly)
- `NN_sub_down.sql`   — subscriber-side rollback

A migration file whose first line is `-- ddl:no-txn` (e.g. `CREATE INDEX CONCURRENTLY`) is applied outside a transaction and without the `SET LOCAL` wrapper. Everything else runs inside a `BEGIN; SET LOCAL lock_timeout='2s'; ... COMMIT;` frame.

## Risks / rollback

- Adds a required CI job. If flakiness surfaces during rollout, mark `logical-replication` as non-required and iterate before enforcing.
- Uses `docker run` rather than the `services:` block in Actions to allow custom `-c wal_level=logical` args. This is standard practice in the Actions community but is a deviation from other jobs in this repo — worth a reviewer eye.
- No changes to production paths. Reverting this PR removes the job with no side effects.

## Reviewer checklist

- [ ] `PARITY_TABLES` auto-detection is acceptable for our publication scope, or an explicit list is set in the workflow env
- [ ] Load parameters (`LOAD_CLIENTS=20`, `LOAD_DURATION=120`) match the size of test databases in CI
- [ ] Postgres major versions in the matrix (`14`, `15`, `16`) match RDS engine versions we run
- [ ] The `Seed baseline schema` step is replaced with the correct bootstrap for this repo
- [ ] `ci/migration_tests/key_queries.yml` has been populated with **your** top-N prod hot queries (the four in the file are placeholders demonstrating format)
- [ ] `PLAN_STRICT` / `PGSS_ENFORCE` defaults match how much noise the team is willing to tolerate on the first few runs
