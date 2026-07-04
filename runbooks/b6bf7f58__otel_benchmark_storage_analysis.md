# OTel Benchmark Artifact Storage: Strategy Comparison

**Context:** `otelcol-logexporter` runs a weekly Monday telemetrygen trace load test with a 10% regression threshold and rolling baselines in `benchmarks/`. Python (Pandas/Matplotlib) generates plots and `benchmarks/reports/regressions.json`. This analysis evaluates three long-term storage strategies for the resulting artifacts (raw JSON, PNG plots, regression reports).

---

## The three options

### 1. GitHub Pages (via `gh-pages` branch)
A dedicated `gh-pages` branch stores benchmark JSON and rendered dashboards; the branch is served as a static site at `https://<user>.github.io/otelcol-logexporter/`. The canonical implementation is [`benchmark-action/github-action-benchmark`](https://github.com/benchmark-action/github-action-benchmark), which appends each run to `dev/bench/data.js` (a JS-wrapped array under `window.BENCHMARK_DATA`) and auto-generates an interactive Chart.js dashboard on first run.

### 2. AWS S3 (object storage)
Raw JSON, plot PNGs, and HTML reports are pushed to a versioned S3 bucket (e.g., `s3://odisena-otel-benchmarks/logexporter/<git-sha>/`). Storage runs $0.023/GB-month for S3 Standard in us-east-1 ([AWS S3 pricing](https://aws.amazon.com/s3/pricing/)), and lifecycle policies can transition older runs to Standard-IA ($0.0125/GB) or Glacier Deep Archive ($0.00099/GB) ([GoCloud 2026 guide](https://go-cloud.io/amazon-s3-pricing/)).

### 3. Git data commits (checked-in JSON in the source repo)
Benchmark JSON is committed directly into a `benchmarks/history/` directory on `main` (or a sibling `benchmarks-data` branch). Every run produces a small commit. Trend visualization is retroactive — a Python script walks `git log` and reconstructs the time series on demand, following the pattern described by [TigerBeetle's devhub](https://ziggit.dev/t/git-benchmarking-workflow/13487) and [Martin Costello's continuous-benchmarks setup](https://blog.martincostello.com/continuous-benchmarks-on-a-budget/).

---

## Decision matrix

| Dimension | GitHub Pages (`gh-pages` branch) | AWS S3 | Git data commits |
|---|---|---|---|
| **Direct cost** | $0 (public repo) | ~$0.023/GB-mo + $0.09/GB egress | $0 (uses existing repo) |
| **Cost at your scale** (weekly runs, ~1 MB JSON + ~500 KB PNGs each → ~75 MB/year) | $0 | <$0.01/month for years | $0 |
| **Break-even vs. self-hosted** | N/A — free tier | ~$50/mo vendor-artifact spend (~200 GB) per [cicdcost.com](https://cicdcost.com/artifact-storage-cost) | N/A |
| **Setup effort** | Low — one action, one orphan branch | Medium — bucket, IAM role, OIDC trust, lifecycle policy | Very low — just `git add && git commit` in the workflow |
| **Data integrity — accidental deletion** | Medium: `gh-pages` branch is force-pushable; a bad workflow can rewrite history. Mitigate with branch protection + `auto-push: false` review gates | High: enable [S3 Versioning + MFA Delete + Object Lock](https://aws.amazon.com/s3/pricing/); lifecycle rules never delete unless explicitly configured | Very high: content is part of `main` history — deletion requires a force-push, which branch protection blocks |
| **Data integrity — tamper evidence** | Medium (commit history on branch) | Medium (versioning + CloudTrail) | Very high (SHA-chained commits, signed if you use `git commit -S`) |
| **Query & analysis** | Fetch `data.js`, parse JS array | `aws s3 cp`, `boto3.client("s3").list_objects_v2` — trivial from Pandas | `git log -- benchmarks/history/*.json` + `git show <sha>:path` — natural fit for your existing scripts |
| **Public dashboard** | Native — free HTTPS static hosting | Requires CloudFront + bucket policy or third-party hosting | None built-in; would need Pages on top anyway |
| **Integration with your Python/Pandas stack** | Good — `data.js` is `window.BENCHMARK_DATA = [...]`, strip prefix and `json.loads` | Excellent — `boto3` + `pd.read_json(s3_uri, storage_options=...)` | Excellent — you already commit `benchmarks/reports/regressions.json`; just add append-only history dir |
| **Repo bloat** | None on `main` (isolated branch) | None | Grows `main` history — ~75 MB/year is negligible, but PNGs bloat clone size faster than JSON |
| **Regression alerting** | Built-in via `alert-threshold` on `benchmark-action/github-action-benchmark` | DIY (you already have this in Python) | DIY (you already have this in Python) |
| **Vendor lock-in** | Medium (GitHub) | Low (S3 API is portable to R2/B2) | None |
| **Best for** | Public dashboards, low-effort visualization | Large binary artifacts (heap dumps, pprof files, flamegraphs) | Small structured JSON where full-history reproducibility matters |

---

## Recommendation for otelcol-logexporter

**Use a hybrid: git data commits for JSON + GitHub Pages for the dashboard, with S3 reserved for large binary artifacts.**

Rationale, tailored to your setup:

1. **You already write `benchmarks/reports/regressions.json`.** Extending that to an append-only `benchmarks/history/<YYYY-MM-DD>_<sha>.json` per run is a two-line change to your existing workflow and gives you the strongest integrity guarantee (protected `main`, SHA-chained history, signed commits if desired).
2. **GitHub Pages is the right dashboard layer** because your consumers are you plus a Slack DM — a public HTML chart at `<user>.github.io/otelcol-logexporter/` is zero-marginal-cost and beats maintaining a CloudFront distribution. Your Python script can render a static `index.html` from the committed history and push it to `gh-pages` in the same job.
3. **Reserve S3 for the heavy stuff.** When you eventually attach pprof heap profiles, flame SVGs, or full telemetrygen packet captures to a run, S3 is 11× cheaper per GB than GitHub-hosted artifacts ([cicdcost.com](https://cicdcost.com/artifact-storage-cost)) and lifecycle rules can auto-archive runs older than 90 days to Glacier Deep Archive at $0.00099/GB-month.
4. **Skip `benchmark-action/github-action-benchmark`'s default alerting.** Its threshold semantics ("200% worse than previous") don't match your 10% rolling-baseline logic — keep your Python regression detector as the source of truth and let the action handle only the chart rendering, or render charts yourself.

---

## Sample workflow — git-commit-to-gh-pages pattern (recommended)

This mirrors your existing weekly cadence, appends the run to committed history on `main`, and republishes the dashboard to `gh-pages`. It assumes your Python scripts live at `benchmarks/scripts/`.

```yaml
# .github/workflows/benchmark.yml
name: Weekly benchmark

on:
  schedule:
    - cron: '0 13 * * 1'   # Mondays 13:00 UTC
  workflow_dispatch:

permissions:
  contents: write       # commit history JSON to main + push gh-pages
  deployments: write    # gh-pages deployment
  pull-requests: write  # optional: comment on PR if run from one

concurrency:
  group: benchmark-${{ github.ref }}
  cancel-in-progress: false   # never cancel a running benchmark

jobs:
  benchmark:
    runs-on: ubuntu-latest
    timeout-minutes: 20
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0   # need history for retroactive trend rebuild

      - uses: actions/setup-go@v5
        with: { go-version: '1.23' }

      - uses: actions/setup-python@v5
        with: { python-version: '3.12' }

      - name: Install Python deps
        run: pip install -r benchmarks/scripts/requirements.txt

      - name: Build collector + telemetrygen
        run: |
          make build
          go install go.opentelemetry.io/collector/cmd/telemetrygen@latest

      - name: Run 5-minute telemetrygen trace load
        run: |
          ./bin/otelcol-logexporter --config benchmarks/config.yaml &
          COLLECTOR_PID=$!
          sleep 5
          telemetrygen traces --rate 5000 --duration 5m --otlp-insecure
          kill $COLLECTOR_PID
        env:
          GOMAXPROCS: 2

      - name: Parse results + detect regressions
        id: analyze
        run: |
          python benchmarks/scripts/analyze.py \
            --input benchmarks/raw/latest.json \
            --history benchmarks/history \
            --threshold 0.10 \
            --output-report benchmarks/reports/regressions.json \
            --output-history "benchmarks/history/$(date -u +%Y-%m-%d)_${GITHUB_SHA::7}.json"

      - name: Render dashboard (Matplotlib → static HTML)
        run: |
          python benchmarks/scripts/render_dashboard.py \
            --history benchmarks/history \
            --out-dir site/

      - name: Commit history JSON to main
        run: |
          git config user.name  "github-actions[bot]"
          git config user.email "41898282+github-actions[bot]@users.noreply.github.com"
          git add benchmarks/history/ benchmarks/reports/regressions.json
          git commit -m "bench: weekly run ${GITHUB_SHA::7} [skip ci]" || echo "No changes"
          git push origin HEAD:${{ github.ref_name }}

      - name: Publish dashboard to gh-pages
        uses: peaceiris/actions-gh-pages@v4
        with:
          github_token: ${{ secrets.GITHUB_TOKEN }}
          publish_dir: ./site
          publish_branch: gh-pages
          keep_files: false        # dashboard is fully regenerated each run
          commit_message: "docs(bench): dashboard for ${{ github.sha }}"

      - name: Upload artifacts (30-day retention safety net)
        uses: actions/upload-artifact@v4
        with:
          name: benchmark-${{ github.run_id }}
          path: |
            benchmarks/raw/
            benchmarks/reports/
          retention-days: 30

      - name: Fail on regression
        if: steps.analyze.outputs.regressed == 'true'
        run: |
          echo "::error::Regression exceeded 10% threshold"
          exit 1
```

**Key integrity properties:**
- `benchmarks/history/*.json` is committed to `main`. Enable branch protection with "Require linear history" + "Do not allow force pushes" and the history is effectively immutable.
- `gh-pages` is fully regenerated each run (`keep_files: false`), so a corrupted dashboard commit can be recovered by re-running the workflow — the source of truth is `main`.
- `[skip ci]` in the bot commit prevents recursive workflow triggers.
- The `actions/upload-artifact` step gives you a 30-day rescue window if the git commit step fails partway.

---

## Alternate workflow — upload-to-S3 pattern

Swap the "Commit history JSON" and "Publish dashboard" steps for the block below when you outgrow git-committed JSON (e.g., adding pprof profiles, or you cross ~1 GB of accumulated artifacts).

```yaml
      - name: Configure AWS credentials (OIDC — no long-lived keys)
        uses: aws-actions/configure-aws-credentials@v4
        with:
          role-to-assume: arn:aws:iam::123456789012:role/otelcol-benchmark-uploader
          aws-region: us-east-1

      - name: Upload run to S3
        env:
          BUCKET: odisena-otel-benchmarks
          PREFIX: logexporter/${{ github.sha }}
        run: |
          aws s3 cp benchmarks/raw/       s3://$BUCKET/$PREFIX/raw/       --recursive
          aws s3 cp benchmarks/reports/   s3://$BUCKET/$PREFIX/reports/   --recursive
          aws s3 cp site/                 s3://$BUCKET/$PREFIX/dashboard/ --recursive \
            --content-type text/html --exclude "*" --include "*.html"
          # Also update a stable "latest" pointer
          aws s3 cp benchmarks/reports/regressions.json \
            s3://$BUCKET/logexporter/latest/regressions.json
```

**Required one-time S3 setup for durability:**
```bash
# Enable versioning — protects against overwrite/delete
aws s3api put-bucket-versioning \
  --bucket odisena-otel-benchmarks \
  --versioning-configuration Status=Enabled

# Lifecycle: transition to IA at 90d, Glacier Deep Archive at 365d, never delete
aws s3api put-bucket-lifecycle-configuration \
  --bucket odisena-otel-benchmarks \
  --lifecycle-configuration file://lifecycle.json

# Block all public access; serve dashboard via CloudFront + OAC if needed
aws s3api put-public-access-block \
  --bucket odisena-otel-benchmarks \
  --public-access-block-configuration \
  "BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true"
```

**IAM trust policy** for GitHub OIDC (the role assumed above) restricts the role to your specific repo and `main` branch, so a fork or feature branch can't push to the bucket.

---

## Migration path

1. **Now (week 1):** Add the `benchmarks/history/*.json` append + `render_dashboard.py` to the existing workflow. Enable branch protection on `main`. Cost: $0.
2. **Later (month 3+):** If you start capturing pprof heap dumps or the history dir exceeds ~200 MB, layer the S3 upload step alongside — S3 becomes the artifact store, git stays the metric-history store.
3. **Never:** Rely on `actions/upload-artifact` alone for historical data. Its default 90-day retention silently erases your baseline.

---

## Sources

- [benchmark-action/github-action-benchmark](https://github.com/benchmark-action/github-action-benchmark) — canonical `gh-pages` benchmark storage pattern
- [Martin Costello — Continuous Benchmarks on a Budget](https://blog.martincostello.com/continuous-benchmarks-on-a-budget/) — GitHub Pages + Actions cost analysis
- [The Bandwidth Trap — cicdcost.com](https://cicdcost.com/artifact-storage-cost) — vendor artifact vs S3 cost breakeven
- [AWS S3 Pricing](https://aws.amazon.com/s3/pricing/) — current per-GB rates
- [GoCloud — S3 Pricing 2026](https://go-cloud.io/amazon-s3-pricing/) — storage class comparison
- [TigerBeetle devhub pattern (Ziggit)](https://ziggit.dev/t/git-benchmarking-workflow/13487) — retroactive git-history benchmarking
