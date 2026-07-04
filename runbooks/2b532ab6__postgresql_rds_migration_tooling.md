# PostgreSQL RDS Migration Tooling

> **Onboarding reference.** This page is the single source of truth for the migration-safety stack: what each component does, how they fit together, and what an engineer needs to know to run, modify, or extend them. If you are new to the team, read this end-to-end once, then keep it bookmarked.

Last updated: 2026-07-02

---

## 1. What this stack exists to prevent

Large PostgreSQL RDS migrations fail in a small number of well-known ways:

- **Long-running DDL under default lock timeouts** — a schema change waits on a lock, holds up every other query, and the API tips over.
- **Non-concurrent index builds** on hot tables — table-level `ShareLock` blocks writes for the duration of the build.
- **`ADD COLUMN NOT NULL DEFAULT <non-constant>`** on large tables — Postgres rewrites the whole table.
- **`ADD FOREIGN KEY` without `NOT VALID` + `VALIDATE CONSTRAINT`** — full table scan under `AccessExclusiveLock`.
- **Bulk `UPDATE` / backfill in a single transaction** — bloats WAL, blocks vacuum, risks replica lag.
- **`ALTER TYPE ADD VALUE` inside a transaction** — cannot be rolled back cleanly.
- **`ALTER COLUMN TYPE` requiring a rewrite** — same table-rewrite problem as `NOT NULL DEFAULT`.
- **`DROP COLUMN` on a hot table** — blocks readers/writers even though it's "instant" logically.
- **Explicit `LOCK TABLE`** — almost always an anti-pattern in migrations.
- **`VACUUM FULL` / `CLUSTER`** — full table rewrite + exclusive lock.
- **`REINDEX` non-concurrent** — same problem, older syntax.

The stack below catches these patterns before they merge, records every attempt in Notion, and surfaces the persistent offenders on a dashboard so we can measure whether hygiene is improving.

---

## 2. Component map

```
  ┌──────────────────────────────────────────────────────────────┐
  │  Developer opens a PR that touches db/migrations/**          │
  └──────────────────────────────────────────────────────────────┘
                              │
                              ▼
  ┌──────────────────────────────────────────────────────────────┐
  │  GH Action: db-migration-preflight.yml                       │
  │    1. verify_rds_migration_config.sh   (config drift)        │
  │    2. preflight_session_hygiene.sh     (session gates)       │
  │    3. format_preflight_output.py       (PR comment)          │
  │    4. log_preflight_to_notion.py       (write run row)       │
  │    5. Slack notification on BLOCKED                          │
  └──────────────────────────────────────────────────────────────┘
                              │
                              ▼
  ┌──────────────────────────────────────────────────────────────┐
  │  Notion: DB Migration Preflight Runs (per-run tracker)       │
  └──────────────────────────────────────────────────────────────┘
                              │
              (weekly Monday 09:00 UTC cron)
                              ▼
  ┌──────────────────────────────────────────────────────────────┐
  │  GH Action: db-migration-dashboard-refresh.yml               │
  │    → dashboard_writer.py                                     │
  │        imports analyze_preflight_history.py                  │
  │        buckets runs by (file, ISO week)                      │
  │        classifies files against 10 DDL anti-patterns         │
  │        ranks by Wilson 95% lower bound                       │
  │        upserts snapshots + leaderboard                       │
  └──────────────────────────────────────────────────────────────┘
                              │
                              ▼
  ┌──────────────────────────────────────────────────────────────┐
  │  Notion: Weekly Snapshots + Offender Leaderboard             │
  │    → Migration Readiness Dashboard page                      │
  │      (chart view + New-This-Week linked view)                │
  └──────────────────────────────────────────────────────────────┘
```

---

## 3. Runbook — `rds_postgres_migration_runbook.md`

The 24 KB runbook is the strategy document that everything else operationalizes. It's split into four parts:

1. **Instance-level tuning targets.** Recommended `shared_buffers`, `work_mem`, `maintenance_work_mem`, `max_wal_size`, `checkpoint_completion_target`, `effective_cache_size`, `wal_buffers`, and `random_page_cost` values as functions of instance class. Also documents which parameters require a reboot vs. which are dynamic.
2. **Session-level hygiene requirements.** What every migration session must set before running DDL: `statement_timeout`, `lock_timeout`, `idle_in_transaction_session_timeout`, `application_name`, and `SET LOCAL` scoping rules.
3. **DDL playbook.** For each anti-pattern in §1, the safe alternative — `CREATE INDEX CONCURRENTLY`, `ADD COLUMN` split into three commits (add nullable, backfill in batches, set NOT NULL with `VALIDATE`), `ADD FOREIGN KEY … NOT VALID` then `VALIDATE CONSTRAINT`, batched backfills using keyset pagination, etc.
4. **Runbook for the actual cutover.** Sequencing, monitoring queries, rollback triggers, replica-lag budget.

The two shell scripts below are the automated enforcement layer for parts 1 and 2. The analyzer/dashboard is the reporting layer for part 3.

---

## 4. Preflight scripts

Both scripts live in `gh-action/scripts/`, are invoked by the GitHub Action, and can also be run locally.

### 4.1 `verify_rds_migration_config.sh` — config drift check

**Purpose.** Verifies that the target RDS instance's server-level parameters match the runbook targets *before* a migration runs, so we don't discover the instance is undersized halfway through a rewrite.

**How it works.**

- Reads target values from a version-controlled YAML/env source (defaults baked into the script).
- Connects with `psql` and runs `SHOW <param>` for each parameter of interest.
- For each parameter: emits `PASS`, `WARN` (within tolerance band), or `FAIL` (outside tolerance).
- Exits `0` on all-PASS, `1` on any FAIL. WARN alone is non-blocking.

**Parameters checked.** `shared_buffers`, `work_mem`, `maintenance_work_mem`, `max_wal_size`, `checkpoint_timeout`, `checkpoint_completion_target`, `effective_cache_size`, `wal_buffers`, `random_page_cost`, `max_connections`, `statement_timeout` (default), `lock_timeout` (default), `idle_in_transaction_session_timeout` (default).

**Common failure modes.**

- Instance was resized but parameters weren't re-tuned → most parameters WARN, a few FAIL.
- Parameter group is set to `default.postgres15` instead of the custom group → almost everything FAIL.
- `max_connections` was raised without raising `shared_buffers` → memory-pressure FAIL.

### 4.2 `preflight_session_hygiene.sh` — session gating

**Purpose.** Verifies that the *migration session itself* is configured safely — this is the layer that catches "someone forgot to set `lock_timeout`" before the migration acquires a lock and blocks production.

**Four gating checks.**

1. **`statement_timeout` is set and ≤ configured ceiling** (default 15 min). Migrations that legitimately need longer must set it explicitly in their SQL and justify it in the PR description.
2. **`lock_timeout` is set and ≤ configured ceiling** (default 5 s). Prevents unbounded waits behind long-running readers.
3. **`idle_in_transaction_session_timeout` is set and ≤ configured ceiling** (default 60 s). Catches migrations that open a transaction and then do slow work outside the DB.
4. **`application_name` is set to a recognizable value** (default matches `^migration[-_]`). Enables per-migration observability in `pg_stat_activity`.

Exits `0` on all-PASS, `1` on any FAIL. There is no WARN tier here — session hygiene is binary.

### 4.3 `format_preflight_output.py`

Small stdlib-only formatter. Takes the raw output from the two shell scripts and produces the Markdown PR comment: pass/fail badges per check, collapsible details, and a summary status.

---

## 5. GitHub Action — `db-migration-preflight.yml`

**Trigger.** `pull_request` on `opened`, `synchronize`, `reopened` — only when files under `db/migrations/**` are touched.

**Job flow.**

1. Checkout, set up Python 3.11 and `psql` client.
2. Run `verify_rds_migration_config.sh`. Capture output + exit code.
3. Run `preflight_session_hygiene.sh`. Capture output + exit code.
4. Compute overall status: `BLOCKED` if any script exited non-zero, `PASS` otherwise. (There is no partial-pass state on the PR comment — that would just get ignored.)
5. Run `format_preflight_output.py` and post/update the PR comment (uses `hashicorp/github-actions-comment-update` pattern with a marker so the comment is idempotent).
6. Run `log_preflight_to_notion.py` to write a run row.
7. If overall status is `BLOCKED`, post a Slack notification to `#db-migrations`.

**Historically important fixes.**

- **`script -qefc` exit-code bug.** Early versions used `script(1)` to capture terminal output; `script` masked the underlying exit code so BLOCKED runs were reported as PASS. Fixed by capturing to a file and reading `${PIPESTATUS[0]}` explicitly.
- **PR title script injection.** PR titles were interpolated directly into shell strings. Fixed by moving all PR metadata through env vars (`env: PR_TITLE: ${{ github.event.pull_request.title }}` and reading `"$PR_TITLE"` in the script).

**Secrets required.**

- `RDS_MIGRATION_DSN` — psql connection string to the pre-prod verifier instance.
- `NOTION_TOKEN` — Notion integration token.
- `NOTION_DATABASE_ID` — the Preflight Runs database.
- `SLACK_WEBHOOK_URL` — incoming webhook for `#db-migrations`.

---

## 6. Notion database schemas

### 6.1 DB Migration Preflight Runs (per-run tracker)

- **Database ID:** `42d763db-6867-4d56-93c7-eae4e2928a31`
- **One row per Action run.**

| Property | Type | Notes |
|---|---|---|
| Run Name | Title | `<repo> PR#<n> — <first migration file>` |
| PR Number | Number | |
| Repository | Rich text | `owner/repo` |
| Overall Status | Select | `PASS` / `BLOCKED` |
| Config Verifier | Select | `PASS` / `WARN` / `FAIL` |
| Session Hygiene | Select | `PASS` / `FAIL` |
| Migration Files | Rich text | Newline-separated list of paths |
| Run Time | Date | UTC timestamp |
| Actions Run URL | URL | Deep link back to the GH Action run |

### 6.2 Preflight Weekly Snapshots

- **Database ID:** `bf4499fc-94e2-4cbb-a421-062c27afb471`
- **One row per (migration file × ISO week).** Populated by the weekly cron; upserted by `Snapshot Key`.

| Property | Type | Notes |
|---|---|---|
| Snapshot Key | Title | `<file>\|<YYYY-Www>` — the upsert key |
| Migration File | Rich text | Full path |
| ISO Week | Rich text | e.g. `2026-W27` |
| Week Start | Date | Monday of the ISO week; the X-axis for the chart |
| Total Runs | Number | |
| Failed Runs | Number | Count of runs where Overall Status ≠ PASS |
| Failure Rate | Number (percent) | `Failed / Total`; the Y-axis for the chart |
| Wilson Lower Bound | Number | 95% Wilson lower bound on failure rate |
| Was In Top 3 | Checkbox | Filter the chart on this to show only persistent offenders |

### 6.3 Offender Leaderboard

- **Database ID:** `40722159-7b35-4f69-831f-4efc20744956`
- **One row per migration file that has ever entered the top-3.** Upserted by `Migration File`.

| Property | Type | Notes |
|---|---|---|
| Migration File | Title | The upsert key |
| Current Rank | Number | 1–3, empty when the file has aged out |
| Wilson Lower Bound | Number | Rolling 12-week |
| Total Runs | Number | Rolling 12-week |
| Failed Runs | Number | Rolling 12-week |
| Anti-patterns Detected | Multi-select | From the analyzer's classification |
| Status | Select | `New` / `Persistent` / `Improving` / `Resolved` |
| New This Week | Checkbox | The dynamic-callout mechanism — see §8 |
| Weeks In Top 3 | Number | Total weeks in top-3 over the rolling window |
| First Seen Week | Rich text | ISO week the file first entered top-3 |
| Last Seen Week | Rich text | Most recent ISO week in top-3 |

---

## 7. Analyzer — `analyze_preflight_history.py`

**Purpose.** Reads the Preflight Runs tracker, classifies migration files against the DDL anti-pattern catalog, and ranks the worst offenders. Runs standalone (produces a Markdown report) and is imported by the dashboard writer (reuses its data model and stats).

**Data model.**

- `Run` dataclass — one row of the tracker after `parse_row()`.
- `AntiPattern` dataclass — `name`, `why`, `filename` regexes, `content` regexes, `trigger` (`hygiene` / `config` / `either`).
- `ANTI_PATTERNS` — the catalog of 10 rules from §1.
- `PATTERN_LABEL_MAP` — shared with the dashboard writer so the multi-select values stay consistent.

**Key functions.**

- `fetch_all_rows(database_id, token, cutoff_date=None)` — paginated Notion REST query; not the MCP tool, native `POST /v1/databases/<id>/query`.
- `parse_row(row)` — schema-aware property extraction from the Notion row shape.
- `classify_file(path, sql_content=None)` — runs each `AntiPattern` against the filename and (if available) the SQL contents. Returns the list of matches. When SQL is unavailable, falls back to filename-only classification.
- `wilson_lower_bound(failed, total, z=1.96)` — 95% Wilson lower bound. Rewards files with more data; prevents a single 1/1 failure from ranking above a 3/50 file.
- `build_recommendations(top_offenders)` — maps each detected anti-pattern to the "safe alternative" paragraph from the runbook, so the report is actionable.

**CLI modes.**

- Default — fetch, classify, rank, print top-N Markdown report to stdout.
- `--dry-run-fixture <path.json>` — read a saved Notion query response instead of hitting the API. Used for local development and CI tests.

---

## 8. Dashboard writer — `dashboard_writer.py`

**Purpose.** Weekly job that turns the raw Preflight Runs tracker into the two derived databases. Runs on cron; safe to re-run manually.

**Flow.**

1. Fetch the last 12 weeks of runs (via the analyzer's `fetch_all_rows`).
2. `bucket_runs(runs)` → `dict[(file, iso_week), WeekBucket]` where `WeekBucket` holds total, failed, and the list of run row IDs for traceability.
3. For each ISO week in the window, compute `top3_by_week(buckets, week)` — sorted by `(wilson_lower_bound, failed, total)` descending, take 3.
4. `upsert_snapshot(bucket, was_in_top_3)` — filter Snapshots by `Snapshot Key` equals `<file>|<week>`, PATCH if found else POST.
5. For each file currently in the top-3 this week: `upsert_leaderboard(file, stats)`.
6. `mark_resolved()` — find leaderboard pages with `Current Rank` set but the file is *not* in this week's top-3; clear the rank, set `Status = Resolved`.

**"New This Week" logic — the dynamic-callout mechanism.**

- If a leaderboard row for `<file>` does not exist → create it with `New This Week = ✅`, `Status = New`.
- If it exists but the row's most recent `Last Seen Week` is more than 1 week behind the current week → the file *re-entered* the top-3; set `New This Week = ✅`, `Status = New`.
- If it exists and was already in the top-3 last week → set `New This Week = ⬜`, promote `Status` (`New → Persistent` after 3 consecutive weeks; drop to `Improving` if Wilson dropped ≥10pp from the row's peak).

Filtering the leaderboard by `New This Week = ✅` gives you the "new entrant" view — it lights up automatically the week a file first breaks in, and clears itself the following week.

**CLI modes.**

- Default — connects to Notion and writes.
- `--dry-run-fixture <path.json>` — computes everything but prints a preview JSON instead of writing. Validated end-to-end against the seed fixture in `fixtures/tracker_seed.json`.
- `--weeks N` — override the rolling window (default 12).

---

## 9. Weekly cron — `db-migration-dashboard-refresh.yml`

- **Schedule.** `"0 9 * * 1"` — Mondays 09:00 UTC (05:00 EDT).
- **Manual trigger.** `workflow_dispatch` with an optional `weeks` input.
- **Idempotent.** Writes upsert by title key, so re-runs are safe. Concurrency group uses `cancel-in-progress: false` for the same reason.
- **Runtime.** 10-minute timeout; the job is small (Python 3.11, `requests`).

**Secrets required (in addition to the preflight action's set).**

- `NOTION_SNAPSHOTS_DB_ID` = `3d217f83-1bb6-487c-9941-2bc611b95a07`
- `NOTION_LEADERBOARD_DB_ID` = `d53aa6bd-1854-4383-b433-855f261506aa`

---

## 10. Dashboard page — Migration Readiness Dashboard

Lives at page ID `391c43ec-8bdd-81ec-8101-edfe7a6903e9`. Section 6 embeds both derived databases and includes step-by-step instructions for the one thing the Notion public API cannot create programmatically: the trend chart block itself.

**Manual chart setup (~30 seconds).**

1. Open **Preflight Weekly Snapshots** on the dashboard.
2. **+ New view** → **Chart**.
3. X-axis: `Week Start` (grouped by Week).
4. Y-axis: `Failure Rate` (Average).
5. Break down by: `Migration File`.
6. Filter: `Was In Top 3` is checked.
7. Sort: `Week Start` ascending.

**Manual "new entrant" callout setup.**

1. On the dashboard page, `/linked` → **Linked view of database** → **Offender Leaderboard**.
2. Filter: `New This Week` is checked.
3. The view is empty during quiet weeks and populates automatically the week a file first breaks in.

---

## 11. Onboarding checklist for a new engineer

- [ ] Read the runbook end-to-end.
- [ ] Clone `db-migration-preflight-action.zip` into any repo that runs migrations. Wire up the four secrets.
- [ ] Open the Migration Readiness Dashboard in Notion. Confirm you can see all three databases.
- [ ] Run `verify_rds_migration_config.sh` locally against the pre-prod DSN — you should see the same output the Action produces.
- [ ] Run `preflight_session_hygiene.sh` locally in "example failure" mode (unset one of the timeouts) — confirm you understand what a FAIL row looks like.
- [ ] Run `analyze_preflight_history.py --dry-run-fixture fixtures/tracker_seed.json` — read the ranked report.
- [ ] Run `dashboard_writer.py --dry-run-fixture fixtures/tracker_seed.json --weeks 12` — read the preview JSON.
- [ ] Watch one real PR go through the pipeline end-to-end. The Notion run row should appear within a minute of the Action finishing.

---

## 12. Files & artifact map

All source lives in `gh-action/` in the migration-safety repo. The current bundle is `db-migration-preflight-action.zip`.

```
gh-action/
├── .github/workflows/
│   ├── db-migration-preflight.yml           # per-PR check
│   └── db-migration-dashboard-refresh.yml   # weekly cron
└── scripts/
    ├── verify_rds_migration_config.sh
    ├── preflight_session_hygiene.sh
    ├── format_preflight_output.py
    ├── log_preflight_to_notion.py
    ├── analyze_preflight_history.py
    ├── dashboard_writer.py
    └── fixtures/
        └── tracker_seed.json
```

Runbook lives separately as `rds_postgres_migration_runbook.md` in the docs repo.

---

## 13. Change history

- **2026-07-02** — Dashboard writer + weekly cron shipped. Two derived Notion databases created. Section 6 appended to the Migration Readiness Dashboard.
- **2026-07-02** — Analyzer shipped with 10-pattern DDL catalog and Wilson-lower-bound ranking.
- **2026-07-02** — Notion logging added to the preflight Action; PR-title script-injection vulnerability closed.
- **2026-07-02** — Preflight Action fixed to correctly propagate exit codes through `script(1)`.
- **2026-07-02** — Initial preflight Action, config verifier, and session hygiene scripts shipped.
- **2026-07-02** — Runbook drafted.
