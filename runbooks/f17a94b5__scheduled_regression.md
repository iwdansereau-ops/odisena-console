# Monday Scheduled Plan-Regression Suite

Catches slow-motion planner drift on the 2 TB RDS fleet before it hits prod.
Unlike the per-migration `plan_regression` check (which compares before/after
one change), this suite runs weekly on Monday morning against staging and
compares each key query's current EXPLAIN plan against a **rolling 30-day
baseline**. Regressions post to Slack with a link to the correlated Notion
DDL Change Log row so on-call can go from alert → probable cause in one
click.

## Architecture

```
Monday 09:00 ET (13:00 UTC via GH Actions cron)
        │
        ▼
scheduled_regression.sh compare
        │
        ├─▶ plan_regression::capture   (reuses existing lib)
        │      └─▶ EXPLAIN JSON per query into _out/monday-<ts>/plans/
        │
        ├─▶ baseline_aware_analyzer.py
        │      ├─ reads rolling 30d registry (S3 or local)
        │      ├─ computes per-query median cost + node count
        │      ├─ compares current vs median
        │      └─ writes report.json
        │
        ├─▶ slack_digest.py
        │      ├─ reads report.json
        │      ├─ queries Notion DDL Change Log (last 7 days)
        │      ├─ correlates each regressed query to a DDL row
        │      └─ posts formatted digest to SLACK_PLAN_WEBHOOK
        │
        └─▶ baseline_manager.py capture
               └─ appends this run to registry, prunes >30d
```

## Files

| Path | Purpose |
|---|---|
| `ci/scheduled_regression/scheduled_regression.sh` | Runner; `compare` (default), `refresh`, `dry-run` |
| `ci/scheduled_regression/lib/baseline_manager.py` | Rolling 30d JSONL registry, S3 or local backend |
| `ci/scheduled_regression/lib/baseline_aware_analyzer.py` | Median-based comparator with structural signals |
| `ci/scheduled_regression/lib/slack_digest.py` | Slack Block Kit digest + Notion DDL correlation |
| `.github/workflows/weekly-plan-regression.yml` | Monday cron + workflow_dispatch |

## Regression signals

A query is flagged as **REGRESS** if any of these trip:

| Kind | Threshold | Meaning |
|---|---|---|
| `cost` | `abs(delta) > 10%` (positive) | total_cost drifted above 30d median |
| `node_count` | `abs(delta) > 10%` (positive) | scan-node count grew vs 30d median |
| `plan_shape` | dominant sig share ≥60% AND current sig differs | planner picked a new plan shape |
| `seq_scan_appeared` | shape changed + Seq Scan present | table is now full-scanned |
| `index_lost` | index present in ≥50% of baseline captures, absent now | index no longer chosen |

Advisory (info only, not counted as regress):

| Kind | Trigger | Meaning |
|---|---|---|
| `shape_flapping` | dominant sig share <40% | baseline itself is unstable — stabilize bind params |
| `cost` improvement | negative delta > threshold | plan got cheaper — sanity-check but not a regression |

## Baseline storage

**S3 backend** (recommended): set `BASELINE_S3_URI=s3://your-bucket/plan-baselines`.
Registry lives at `<uri>/plan_baseline.jsonl`. One record per (query, capture).

**Local backend** (default, workspace/dev use): set `BASELINE_DIR` (default
`./baselines`). Registry file `plan_baseline.jsonl`.

Retention: on every `capture` and `prune`, records older than 30 days are
dropped. Window is configurable via `BASELINE_WINDOW_DAYS`.

Record schema:
```json
{
  "query": "user_by_email",
  "captured_at": "2026-07-01T13:00:00+00:00",
  "label": "monday-20260701-130000",
  "total_cost": 8.42,
  "scan_count": 1,
  "plan_signature": "9f2ac1b8d4e5f003",
  "indexes_used": ["users_email_idx"]
}
```

## Configuration

### GitHub Actions secrets (repo settings)
- `STAGING_PUB_URI` — psql URI to the staging publisher/primary
- `NOTION_TOKEN` — Notion integration token (DDL log access)
- `SLACK_PLAN_WEBHOOK` — Slack incoming webhook for `#rds-plan-regressions`
- `AWS_ROLE_ARN` — role assumed for S3 baseline reads/writes

### GitHub Actions vars
- `DDL_LOG_DS_ID = c329a821-e7db-4381-9d7a-b414f72adbcd`
- `BASELINE_S3_URI = s3://<bucket>/plan-baselines` *(optional)*
- `AWS_REGION` *(optional)*

### Environment tunables
- `PLAN_COST_THRESHOLD_PCT` (default 10)
- `PLAN_NODES_THRESHOLD_PCT` (default 10)
- `BASELINE_WINDOW_DAYS` (default 30)

## Modes

```bash
# Normal Monday run (cron default)
ci/scheduled_regression/scheduled_regression.sh compare

# Rare: reset the baseline (e.g., after a major schema overhaul)
ci/scheduled_regression/scheduled_regression.sh refresh

# Post to stdout instead of Slack (safe rehearsal)
ci/scheduled_regression/scheduled_regression.sh dry-run
```

Manual GitHub trigger: `Actions → weekly-plan-regression → Run workflow →
choose mode`.

## Bootstrap: first Monday you turn it on

The first run has no baseline, so every query returns `no_baseline`. The
digest reports that state cleanly (no false alarms). After ~4 weeks of
Monday captures, the median stabilizes.

To accelerate: run `refresh` once and then `compare` daily for two weeks
via `workflow_dispatch`. Delete the daily runs from the cron path after
seeding.

## Query registry

Defaults to `ci/migration_tests/key_queries.yml` (4 example queries). If
`ci/migration_tests/prod_shapes.yml` exists (top-20 pg_stat_statements
mapping), it takes precedence automatically. Registry format matches the
existing per-migration harness — no separate schema.

## Remediation playbook

When a query is flagged:

1. **Open the Slack digest, click the DDL Change Log link.** Was a schema
   change deployed in the past 7 days that touches this query's tables? If
   yes, that's your suspect.
2. **Run `ANALYZE <table>`** on the tables involved. Stale stats cause ~30%
   of flapping alerts.
3. **Re-run the suite manually**
   (`Actions → weekly-plan-regression → Run workflow → compare`). Confirm
   the regression persists.
4. **If plan_shape or seq_scan_appeared**, check `pg_stat_user_indexes` on
   the affected table for `idx_scan = 0` — someone likely dropped or made
   an index unusable.
5. **If cost only, no structural signal**, wait one more Monday. Isolated
   cost drift often self-corrects after autovacuum.
6. **If persists 2 Mondays in a row**, roll back the last migration in the
   window and open an incident.

## Notion DDL correlation

`slack_digest.py` queries the DDL Change Log data source
(`c329a821-e7db-4381-9d7a-b414f72adbcd`) for rows with `Start Time` in the
last 7 days. It matches a regressed query to a DDL row by looking for the
query id in the row title or the `Related Alerts` rich_text. If no match,
it links the most recent DDL row (weakest signal — sanity check applies).

## Rolling upgrade path from `key_queries.yml` → `prod_shapes.yml`

The top-20 pg_stat_statements mapping (paste-driven) is still pending. Once
you supply the raw pg_stat_statements top-20 output, the mapping is:

1. Emit `ci/migration_tests/prod_shapes.sql` (canonicalized SQL bodies).
2. Emit `ci/migration_tests/prod_shapes.yml` (same schema as
   `key_queries.yml`, one entry per query with realistic bind values).
3. Commit both. The Monday runner detects `prod_shapes.yml` and switches
   automatically — no workflow change needed.
