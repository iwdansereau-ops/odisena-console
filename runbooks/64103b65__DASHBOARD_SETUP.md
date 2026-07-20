# OTel Perf Dashboard — Persistent Setup

Persistent, hands-off visibility into the 17-metric performance budget. Refreshes daily; emails you on Mondays only if a resource metric shows statistically significant monotonic drift over the last 30 days.

## Architecture

```
                 ┌──────────────────────────────────────┐
  otel-perf-     │ nightly gate uploads per-run         │
  nightly.yml    │ artifact: otel-perf-scorecard-<id>   │
                 └───────────────┬──────────────────────┘
                                 │ (last 30 successful)
                                 ▼
                 ┌──────────────────────────────────────┐
  otel-perf-     │ 07:30 UTC daily:                     │
  dashboard.yml  │  1. gh run download × 30             │
                 │  2. analyze_trend.py  (Theil-Sen/MK) │
                 │  3. build_dashboard.py (static HTML) │
                 │  4. deploy-pages       ─────────────►│ GitHub Pages URL
                 │  5. mirror_to_notion.py (optional) ─►│ Notion page
                 │                                      │
                 │ Mon 14:00 UTC additionally:          │
                 │  6. weekly_digest.py  ─────────────► │ Email (SendGrid/SMTP)
                 │     └─ falls back to filing an issue │
                 └──────────────────────────────────────┘
```

The dashboard job is fully idempotent — it wipes and rebuilds every day.

## Files

Copy to your repo at these paths:

| Repo path | Source |
|---|---|
| `.github/workflows/otel-perf-dashboard.yml` | `.github-workflows-otel-perf-dashboard.yml` |
| `scripts/perf/analyze_trend.py` | `scripts/analyze_trend.py` |
| `scripts/perf/evaluate_budget.py` | `scripts/evaluate_budget.py` |
| `scripts/perf/build_dashboard.py` | `scripts/build_dashboard.py` |
| `scripts/perf/weekly_digest.py` | `scripts/weekly_digest.py` |
| `scripts/perf/mirror_to_notion.py` | `scripts/mirror_to_notion.py` (optional) |
| `perf-budget.yaml` | `perf-budget.yaml` |

Fix up the import path in `analyze_trend.py` and `build_dashboard.py` if you move them.

## One-time repo configuration

### 1. Enable GitHub Pages

Settings → Pages → **Source:** GitHub Actions. That's the only click required — the workflow's `actions/deploy-pages@v4` step handles the rest, and each run outputs the page URL as `steps.pages.outputs.page_url`. The URL is stable across runs.

### 2. Configure secrets

Settings → Secrets and variables → Actions → New repository secret. Only `DIGEST_TO` plus one delivery method is strictly required.

**Required for weekly digest email:**

- `DIGEST_TO` — the recipient inbox for the digest (e.g. a team/role alias like `alerts@example.com`)
- `DIGEST_FROM` — sender identity (defaults to `DIGEST_TO` if unset)

**Choose one email transport:**

| Transport | Secrets |
|---|---|
| SendGrid | `SENDGRID_API_KEY` |
| SMTP (Gmail app password, SES, etc.) | `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASS` |

If no transport is configured, the digest runs anyway, uploads `digest.html` and `digest.txt` as an artifact, and files a GitHub Issue when there are alerts — so you never lose the signal even without email set up.

**Optional Notion mirror:**

- `NOTION_TOKEN` — internal integration token from [notion.so/my-integrations](https://www.notion.so/my-integrations)
- `NOTION_PAGE_ID` — 32-char page ID of a page you've shared with the integration

The mirror step is skipped automatically when either is empty.

### 3. Seed the dashboard

The workflow expects at least 8-10 successful `otel-perf-nightly.yml` runs on `main` before the analysis produces meaningful drift signals (Mann-Kendall τ is undefined below n=3 and unreliable below n≈10). Two options:

- **Wait it out.** Let the nightly workflow run for ~10 days on `main`, then the dashboard fills in.
- **Bootstrap with `workflow_dispatch`.** Manually trigger `otel-perf-nightly.yml` several times back-to-back — each successful run uploads a scorecard artifact that the dashboard job will pick up.

The dashboard job runs safely with fewer than 30 artifacts; it just uses whatever is available.

## Alert criteria (weekly digest)

The digest emails you **only** when at least one of these resource metrics passes all three tests over the 30-day window:

- `cpu_ns_per_span`, `cpu_peak_percent`, `heap_bytes_per_span`, `rss_peak_mib`, `gc_pause_p99_ms`, `queue_saturation_max`
- Kendall τ ≥ 0.30 (consistent monotonic trend, not sawtooth)
- Mann-Kendall p ≤ 0.05 (statistically significant)
- ≥ 15% of the metric's fail-threshold budget consumed by the fitted trend

Latency and throughput metrics are visible on the dashboard but excluded from this specific alert — they have their own signals (queue saturation, retry ratio) that are usually more reliable than raw latency drift.

Adjust `RESOURCE_METRICS`, `TAU_MIN`, `P_MAX`, `BUDGET_MIN` at the top of `weekly_digest.py` if your bias is different.

## Weekly digest silence policy

If no metric qualifies, **no email is sent**. This is intentional — a "nothing to see" digest trains people to filter the alert. The dashboard itself is still refreshed and remains the source of truth for at-a-glance state.

## Choosing GitHub Pages vs Notion vs both

- **GitHub Pages (recommended primary).** Static, self-contained HTML with embedded SVG sparklines and a base64-encoded heatmap. No JS runtime, works offline, permalinked, versioned via the deploy-pages artifact.
- **Notion mirror (optional secondary).** A simplified block-based view (alerts + metric list, no inline chart). Useful if perf reviews happen inside a Notion doc and you want the summary appear alongside notes. The mirror wipes and re-appends its target page each run — do not put non-generated content on that page.

Enabling both is fine; they don't interfere.

## Local dev / dry run

```bash
# From the repo root, after populating runs/ with real or synthetic data:
python scripts/perf/analyze_trend.py
python scripts/perf/build_dashboard.py
# Open dashboard/site/index.html in a browser

# Test the digest without sending email
DIGEST_TO="" python scripts/perf/weekly_digest.py
cat results/digest.txt
```

## Known limitations

- **The workflow pulls artifacts filtered to `event: schedule` runs on `main`.** PR runs of `otel-perf-nightly` are ignored so noisy PR data doesn't skew the baseline. If your production baseline comes from a different event or branch, edit the `gh run list` filters in the workflow.
- **GitHub Pages is public** unless you're on Enterprise with private Pages enabled. If your metric names or baseline values are sensitive, use the Notion mirror path instead and skip the `deploy-pages` step.
- **Mann-Kendall is a trend test, not an outlier detector.** A single very bad day won't move τ enough to alert. That case is what the nightly gate is for.
