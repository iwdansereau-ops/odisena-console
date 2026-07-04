# Notion Migration Tracker — Database Template Spec

Copy this into Notion as **three linked databases**. They connect through Notion relation properties so a single migration record fans out to per-service and per-replica state rows.

---

## Database 1 — `Migrations` (main record, one row per migration)

| Property | Type | Configuration |
|---|---|---|
| Migration ID | Title | Format `YYYY-MM-DD-<slug>` |
| Title | Text | Human-readable summary |
| Status | Status | Groups: **To do** (Draft, Design review) · **In progress** (Expand, Dual-write, Backfill, Verify, Dual-read, Contract) · **Complete** (Done, Rolled back) |
| Owner | Person | Single DRI |
| Phase entered at | Date | Auto-updated when Status changes |
| Soak until | Formula | `dateAdd(prop("Phase entered at"), prop("Soak hours"), "hours")` |
| Soak hours | Number | Per-phase soak requirement (24 for dual-write, 168 for dual-read, etc.) |
| Ready to advance | Formula | `now() > prop("Soak until") and prop("Blockers") == 0` |
| Design doc | URL | Link to design doc (Notion or Google Doc) |
| Blast radius | Text | Table + row count + peak QPS |
| Audit red flags | Multi-select | Options match §3 of the playbook: `Rename column`, `Rename table`, `Narrow type`, `NOT NULL no default`, `Constraint no NOT VALID`, `Non-concurrent index`, `Drop column`, `Change PK`, `Enum drop/rename`, `Non-constant default`, `VACUUM FULL / CLUSTER`, `Partition parent DDL` |
| Rollback plan | Text | Free text, required before Contract |
| PITR marker | Text | Filled at Phase 6 start |
| Expand PR | URL | |
| Dual-write PR | URL | |
| Dual-read PR | URL | |
| Contract PR | URL | |
| Services | Relation → `Migration × Service` | Rollup below |
| Replicas | Relation → `Migration × Replica` | Rollup below |
| Services green | Rollup | Count of related service rows where `Cutover complete = true` |
| Services total | Rollup | Count of related service rows |
| Replicas green | Rollup | Count of related replica rows where `Verify signoff = true` |
| Replicas total | Rollup | Count of related replica rows |
| Blockers | Rollup | Count of related rows where `Blocked = true` |
| Notes | Text | |

**Recommended views:**

- **Board by Status** — Kanban across the phase pipeline.
- **Table: Ready to advance** — filter `Ready to advance = true AND Status != Done`.
- **Table: Waiting on services** — filter `Services green < Services total`.
- **Timeline** — `Phase entered at` on the x-axis, grouped by Owner.
- **Gallery: Red-flag migrations** — filter `Audit red flags is not empty`.

---

## Database 2 — `Migration × Service` (rollout matrix, one row per service per migration)

| Property | Type | Configuration |
|---|---|---|
| Key | Title | Auto: `<migration_id> · <service>` |
| Migration | Relation → `Migrations` | |
| Service | Relation → `Services` catalog (optional) or Select | |
| Role | Select | `Reader`, `Writer`, `Reader+Writer` |
| Owner | Person | |
| Expand deployed | Checkbox | New DDL-aware code shipped |
| Dual-write % | Number (percent) | 0–100 |
| Shadow-read % | Number (percent) | 0–100 |
| New-read % | Number (percent) | 0–100 |
| Old code removed | Checkbox | |
| Cutover complete | Formula | `prop("New-read %") == 100 and prop("Old code removed")` (adjust for readers-only vs. writers-only) |
| Blocked | Checkbox | |
| Blocker note | Text | Required when `Blocked = true` |
| Last updated | Last edited time | |

**Views:**

- **Table grouped by Migration** — shows rollout matrix like §4 of the playbook.
- **Table filtered `Blocked = true`** — active blockers across all migrations.

---

## Database 3 — `Migration × Replica` (per-replica DDL/backfill state)

| Property | Type | Configuration |
|---|---|---|
| Key | Title | Auto: `<migration_id> · <replica>` |
| Migration | Relation → `Migrations` | |
| Replica | Select | `primary`, `replica-1`, `replica-eu`, `analytics-follower`, etc. |
| Region | Select | `us-east-1`, `us-west-2`, `eu-west-1`, ... |
| DDL applied | Checkbox | |
| DDL applied at | Date (with time) | |
| Replay lag at apply | Number (seconds) | |
| Backfill % | Number (percent) | 0–100 |
| Verify signoff | Checkbox | |
| Verify signoff by | Person | |
| Notes | Text | |

**Views:**

- **Table grouped by Migration** — replica status like §4 of the playbook.
- **Table filtered `DDL applied = false`** — replicas still to receive the change.
- **Table filtered `Backfill % < 100`** — backfill progress dashboard.

---

## Optional Database 4 — `Services` (service catalog)

If you don't already maintain one. Very lightweight.

| Property | Type |
|---|---|
| Name | Title |
| Repo | URL |
| Owner team | Select |
| Primary language | Select |
| Reads tables | Multi-select |
| Writes tables | Multi-select |
| On-call channel | Text |

Relate `Migration × Service.Service` to this so a new migration auto-suggests affected services based on `Writes tables` / `Reads tables`.

---

## Automation suggestions (Notion buttons / integrations)

- **"Advance phase" button** on `Migrations` — checks `Ready to advance`, if true increments `Status` to the next phase and stamps `Phase entered at = now()`.
- **Daily digest** — filter `Migrations` where `Status != Done` and post a summary to Slack `#db-migrations`. Uses your existing Slack connector.
- **Auto-create rollout rows** — when a migration is created, a Notion automation creates one `Migration × Service` row per active service in the catalog. Owner then prunes irrelevant ones.
- **Guardrail on Contract** — a formula property `Contract allowed` = `Services green == Services total AND Replicas green == Replicas total AND now() - Phase entered at > 7 days`. Contract PR checklist references it.

---

## Minimal starter (skip the multi-DB setup)

If you'd rather start with a single Notion database, use `Migrations` alone and store the service rollout matrix as a Notion table inside each migration's page body. You lose rollups and cross-migration filtering, but setup is 5 minutes instead of 20.
