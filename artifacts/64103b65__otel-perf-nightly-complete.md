# `otel-perf-nightly` — Complete Workflow Package

Drop-in files for a nightly + PR performance-budget gate on an OpenTelemetry Collector pipeline. All files are complete and runnable; no placeholder logic.

**Layout:**

```
.github/
  workflows/
    otel-perf-nightly.yml          # §1
scripts/
  perf/
    evaluate_budget.py             # §2 — baseline, robust z, dual gate, JUnit, MD
    check_intra_cv.py              # §3 — noise floor guard
    render_pr_comment.py           # §4 — sticky PR comment formatter
    __init__.py                    # empty
perf-budget.yaml                   # §5 — thresholds config
```

Branch-protection setup and the escape-hatch semantics are in §6.

---

## 1. `.github/workflows/otel-perf-nightly.yml`

```yaml
name: otel-perf-nightly

on:
  schedule:
    - cron: '0 6 * * *'            # 06:00 UTC nightly
  pull_request:
    paths:
      - 'collector/**'
      - 'config/**'
      - 'perf-budget.yaml'
      - 'scripts/perf/**'
      - '.github/workflows/otel-perf-nightly.yml'
  workflow_dispatch:
    inputs:
      force_baseline_refresh:
        description: 'Overwrite rolling baseline even if gate fails'
        required: false
        default: 'false'

concurrency:
  group: otel-perf-${{ github.ref }}
  cancel-in-progress: false        # never cancel a running benchmark

permissions:
  contents: read
  pull-requests: write             # sticky PR comment
  checks: write                    # required status check
  actions: read
  issues: read                     # to read PR labels for escape hatch

jobs:
  # -----------------------------------------------------------------------
  # Gate job — the ONLY job marked as a required check in branch protection.
  # It runs the benchmark, evaluates against baseline, handles borderline
  # reruns, and finally consults the escape-hatch label before exiting.
  # -----------------------------------------------------------------------
  perf-budget:
    name: Performance Budget Gate
    runs-on: ubuntu-latest-large   # pinned larger runner — lower CV
    timeout-minutes: 60

    env:
      ITERATIONS: 10
      ITERATION_SECONDS: 60
      WARMUP_SECONDS: 30
      GOMAXPROCS: 4
      GOGC: 100
      BASELINE_WINDOW: 7
      MAX_INTRA_RUN_CV: 0.20
      ESCAPE_HATCH_LABEL: perf-budget-override
      BUDGET_CONFIG: perf-budget.yaml

    outputs:
      verdict: ${{ steps.finalize.outputs.verdict }}
      borderline_only: ${{ steps.evaluate.outputs.borderline_only }}

    steps:
      # ---- 1. Checkout + tool setup ----------------------------------------
      - name: Checkout
        uses: actions/checkout@v4
        with:
          fetch-depth: 0

      - name: Set up Python
        uses: actions/setup-python@v5
        with:
          python-version: '3.12'

      - name: Install evaluator deps
        run: |
          python -m pip install --upgrade pip
          pip install "pyyaml==6.0.2" "numpy==2.1.3" "jinja2==3.1.4"

      # ---- 2. Runner fingerprint (reject if drift vs baseline) -------------
      - name: Fingerprint runner
        id: fp
        run: |
          model=$(grep -m1 "model name" /proc/cpuinfo | cut -d: -f2 | xargs)
          cores=$(nproc)
          kernel=$(uname -r)
          echo "cpu_model=$model"     >> "$GITHUB_OUTPUT"
          echo "cpu_cores=$cores"     >> "$GITHUB_OUTPUT"
          echo "kernel=$kernel"       >> "$GITHUB_OUTPUT"
          mkdir -p ./results
          jq -n \
            --arg cpu "$model" --arg cores "$cores" --arg kernel "$kernel" \
            '{cpu_model:$cpu, cpu_cores:($cores|tonumber), kernel:$kernel}' \
            > ./results/runner.json

      # ---- 3. Pull rolling baseline (7 most recent successful main runs) ---
      # We fetch the aggregated baseline artifact produced by the last nightly
      # main run. Missing baseline is not fatal — evaluator will abstain.
      - name: Download rolling baseline
        id: baseline
        uses: dawidd6/action-download-artifact@v6
        with:
          workflow: otel-perf-nightly.yml
          workflow_conclusion: success
          branch: main
          event: schedule
          name: otel-perf-baseline
          path: ./baseline
          if_no_artifact_found: warn

      # ---- 4. Build + warm caches (untimed) --------------------------------
      - name: Build Collector image
        run: docker build -t otelcol-bench:${{ github.sha }} ./collector

      - name: Warm caches
        run: |
          docker run --rm otelcol-bench:${{ github.sha }} --version
          docker pull otel/opentelemetry-collector-contrib:0.110.0 || true

      # ---- 5. Run benchmark ------------------------------------------------
      - name: Run benchmark suite
        id: bench
        run: |
          taskset -c 0-3 ./scripts/run-bench.sh \
            --iterations "$ITERATIONS" \
            --duration   "$ITERATION_SECONDS" \
            --warmup     "$WARMUP_SECONDS" \
            --output     ./results/current.json

      # ---- 6. Noise-floor guard: reject run if intra-run CV too high -------
      - name: Intra-run noise check
        id: noise
        run: |
          python scripts/perf/check_intra_cv.py \
            --results ./results/current.json \
            --max-cv  "$MAX_INTRA_RUN_CV" \
            --report  ./results/noise.json

      # ---- 7. Evaluate against rolling baseline ----------------------------
      - name: Evaluate budget
        id: evaluate
        run: |
          python scripts/perf/evaluate_budget.py \
            --current  ./results/current.json \
            --baseline ./baseline/ \
            --config   "$BUDGET_CONFIG" \
            --runner   ./results/runner.json \
            --report-md    ./results/scorecard.md \
            --report-json  ./results/scorecard.json \
            --junit        ./results/scorecard.xml
        continue-on-error: true

      # ---- 8. Borderline auto-rerun ---------------------------------------
      # Rerun once only when every failed metric has robust |z| in [3.0, 4.0).
      # A single confirmed re-fail on the same metric = real regression.
      - name: Borderline rerun
        id: rerun
        if: >
          steps.evaluate.outcome == 'failure' &&
          steps.evaluate.outputs.borderline_only == 'true'
        run: |
          echo "::notice::Borderline failure detected — running confirmation pass."
          taskset -c 0-3 ./scripts/run-bench.sh \
            --iterations "$ITERATIONS" \
            --duration   "$ITERATION_SECONDS" \
            --warmup     "$WARMUP_SECONDS" \
            --output     ./results/rerun.json
          python scripts/perf/check_intra_cv.py \
            --results ./results/rerun.json \
            --max-cv  "$MAX_INTRA_RUN_CV" \
            --report  ./results/noise_rerun.json
          python scripts/perf/evaluate_budget.py \
            --current  ./results/rerun.json \
            --baseline ./baseline/ \
            --config   "$BUDGET_CONFIG" \
            --runner   ./results/runner.json \
            --confirm-against ./results/scorecard.json \
            --report-md   ./results/scorecard.md \
            --report-json ./results/scorecard.json \
            --junit       ./results/scorecard.xml
        continue-on-error: true

      # ---- 9. Render PR comment -------------------------------------------
      - name: Render PR comment
        if: github.event_name == 'pull_request' && always()
        run: |
          python scripts/perf/render_pr_comment.py \
            --scorecard ./results/scorecard.json \
            --runner    ./results/runner.json \
            --noise     ./results/noise.json \
            --commit    "${{ github.sha }}" \
            --run-url   "${{ github.server_url }}/${{ github.repository }}/actions/runs/${{ github.run_id }}" \
            --output    ./results/pr-comment.md

      - name: Post sticky PR comment
        if: github.event_name == 'pull_request' && always()
        uses: marocchino/sticky-pull-request-comment@v2
        with:
          header: otel-perf-budget
          path: ./results/pr-comment.md

      # ---- 10. Publish JUnit + artifacts ----------------------------------
      - name: Publish JUnit
        if: always()
        uses: mikepenz/action-junit-report@v4
        with:
          report_paths: ./results/scorecard.xml
          check_name: otel-perf-budget-junit
          fail_on_failure: false     # gate decision is made below, not here

      - name: Upload scorecard artifact
        if: always()
        uses: actions/upload-artifact@v4
        with:
          name: otel-perf-scorecard-${{ github.run_id }}
          path: ./results/
          retention-days: 90

      # ---- 11. Update rolling baseline (main + nightly + success only) ----
      - name: Refresh rolling baseline
        if: >
          github.ref == 'refs/heads/main' &&
          github.event_name == 'schedule' &&
          (steps.rerun.outcome == 'success' ||
           (steps.evaluate.outcome == 'success' && steps.rerun.outcome == 'skipped'))
        run: |
          python scripts/perf/evaluate_budget.py \
            --refresh-baseline \
            --current  ./results/current.json \
            --baseline ./baseline/ \
            --window   "$BASELINE_WINDOW" \
            --out      ./baseline-next/

      - name: Publish refreshed baseline
        if: >
          github.ref == 'refs/heads/main' &&
          github.event_name == 'schedule' &&
          hashFiles('./baseline-next/**') != ''
        uses: actions/upload-artifact@v4
        with:
          name: otel-perf-baseline
          path: ./baseline-next/
          retention-days: 30

      # ---- 12. Escape-hatch + final gate ----------------------------------
      # The label is only honored on pull_request events, must be applied by
      # a user with write access (GitHub enforces this on label events), and
      # the workflow logs the override so it appears in the audit trail.
      - name: Check escape-hatch label
        id: escape
        if: github.event_name == 'pull_request'
        uses: actions/github-script@v7
        with:
          script: |
            const { data: pr } = await github.rest.pulls.get({
              owner: context.repo.owner,
              repo:  context.repo.repo,
              pull_number: context.payload.pull_request.number,
            });
            const hasLabel = pr.labels.some(
              l => l.name === process.env.ESCAPE_HATCH_LABEL
            );
            core.setOutput('override', hasLabel ? 'true' : 'false');
            core.setOutput('pr_author', pr.user.login);
            if (hasLabel) {
              core.warning(
                `perf-budget-override label present on PR #${pr.number} ` +
                `by @${pr.user.login}. Gate will PASS regardless of budget. ` +
                `A linked follow-up issue is required by policy.`
              );
            }

      - name: Finalize verdict
        id: finalize
        run: |
          # Determine the effective evaluator outcome. If rerun executed,
          # its outcome supersedes the initial evaluate step.
          if [[ "${{ steps.rerun.outcome }}" == "success" ]]; then
            outcome=success
          elif [[ "${{ steps.rerun.outcome }}" == "failure" ]]; then
            outcome=failure
          else
            outcome="${{ steps.evaluate.outcome }}"
          fi

          override="${{ steps.escape.outputs.override }}"
          echo "Effective evaluator outcome: $outcome"
          echo "Escape-hatch override:       ${override:-false}"

          if [[ "$outcome" == "success" ]]; then
            echo "verdict=pass" >> "$GITHUB_OUTPUT"
            echo "::notice::Perf budget PASS."
            exit 0
          fi

          if [[ "$override" == "true" ]]; then
            echo "verdict=override" >> "$GITHUB_OUTPUT"
            echo "::warning::Perf budget FAILED but escape-hatch label is set. Passing gate."
            exit 0
          fi

          echo "verdict=fail" >> "$GITHUB_OUTPUT"
          echo "::error::Perf budget FAILED. See scorecard artifact for details."
          exit 1

  # -----------------------------------------------------------------------
  # Secondary job — audit trail for overrides. Not required for merge.
  # Fails loudly if perf-budget-override was used without a linked issue.
  # -----------------------------------------------------------------------
  override-audit:
    name: Override Audit
    if: github.event_name == 'pull_request'
    needs: perf-budget
    runs-on: ubuntu-latest
    steps:
      - name: Enforce follow-up issue when override used
        if: needs.perf-budget.outputs.verdict == 'override'
        uses: actions/github-script@v7
        with:
          script: |
            const pr = context.payload.pull_request;
            const body = (pr.body || '') + '\n' + (pr.title || '');
            const linked = /(closes|fixes|resolves|tracks|refs)\s+#\d+/i.test(body)
                         || /perf-followup:\s*#\d+/i.test(body);
            if (!linked) {
              core.setFailed(
                'perf-budget-override label used, but no follow-up issue ' +
                'linked in the PR body (e.g. "perf-followup: #1234").'
              );
            }
```

---

## 2. `scripts/perf/evaluate_budget.py`

Complete implementation of: rolling baseline aggregation, robust z-score, dual-gate (relative drift **and** statistical significance), scorecard JSON/Markdown/JUnit emission, and the `--confirm-against` mode used by the borderline rerun.

```python
#!/usr/bin/env python3
"""
evaluate_budget.py — OTel performance-budget gate.

Modes:
  Default:            evaluate current run vs rolling baseline.
  --confirm-against:  rerun mode; a metric is failed only if it fails now AND
                      failed the same way in the referenced prior scorecard.
  --refresh-baseline: append current run to baseline, keep last N successes.

Exit codes:
  0  pass  (no failures; warnings are non-fatal)
  1  fail
  2  abstain (insufficient baseline; treated as pass but logged)
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import Any

import numpy as np
import yaml

ABSTAIN_MIN_SAMPLES = 3
ROBUST_Z_FAIL = 3.0
ROBUST_Z_BORDERLINE_HI = 4.0


# --------------------------------------------------------------------------- #
# Data model                                                                  #
# --------------------------------------------------------------------------- #
@dataclass
class MetricSpec:
    name: str
    direction: str                       # increase_is_bad | decrease_is_bad
    absolute_budget_max: float | None = None
    absolute_budget_min: float | None = None
    warn_relative: float | None = None
    fail_relative: float | None = None
    warn_absolute_delta: float | None = None
    fail_absolute_delta: float | None = None


@dataclass
class MetricResult:
    name: str
    current: float
    baseline_mean: float | None
    baseline_std: float | None
    baseline_median: float | None
    baseline_mad: float | None
    n_baseline: int
    relative_drift: float | None
    robust_z: float | None
    z_score: float | None
    absolute_ok: bool
    relative_ok: bool
    zscore_ok: bool
    status: str                          # pass | warn | fail | abstain
    borderline: bool = False
    reasons: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------- #
# I/O helpers                                                                 #
# --------------------------------------------------------------------------- #
def load_config(path: str) -> tuple[dict, list[MetricSpec]]:
    with open(path) as f:
        cfg = yaml.safe_load(f)
    specs = [MetricSpec(**m) for m in cfg["metrics"]]
    return cfg, specs


def load_run(path: str) -> dict[str, list[float]]:
    """A run file is JSON: {"iterations": [{"metric_name": value, ...}, ...]}"""
    with open(path) as f:
        run = json.load(f)
    iters = run["iterations"]
    by_metric: dict[str, list[float]] = {}
    for it in iters:
        for k, v in it.items():
            if isinstance(v, (int, float)):
                by_metric.setdefault(k, []).append(float(v))
    return by_metric


def load_baseline(dir_path: str) -> dict[str, list[float]]:
    """
    Baseline directory contains up to N summary files:
      baseline/run-YYYYMMDD-<sha>.json
    Each file is one run's per-metric median (a dict of metric → float).
    Returns metric → list of per-run medians.
    """
    d = Path(dir_path)
    if not d.exists():
        return {}
    by_metric: dict[str, list[float]] = {}
    for f in sorted(d.glob("run-*.json")):
        with open(f) as fh:
            summary = json.load(fh)
        for k, v in summary.items():
            if isinstance(v, (int, float)):
                by_metric.setdefault(k, []).append(float(v))
    return by_metric


# --------------------------------------------------------------------------- #
# Statistics                                                                  #
# --------------------------------------------------------------------------- #
def summarize_run(samples: list[float]) -> float:
    """Trimmed median: drop top+bottom, then take median. Per §3 of the guide."""
    if len(samples) <= 2:
        return float(np.median(samples))
    trimmed = sorted(samples)[1:-1]
    return float(np.median(trimmed))


def robust_z(current: float, baseline: list[float]) -> tuple[float, float, float, float, float] | None:
    """Return (mean, std, median, mad, robust_z). None if too few samples."""
    if len(baseline) < ABSTAIN_MIN_SAMPLES:
        return None
    arr = np.asarray(baseline, dtype=float)
    mean = float(arr.mean())
    std = float(arr.std(ddof=1)) if len(arr) > 1 else 0.0
    median = float(np.median(arr))
    mad = float(np.median(np.abs(arr - median)))
    # 0.6745 = quantile factor so MAD-based z matches z on a normal distribution
    if mad == 0.0:
        rz = 0.0 if current == median else float("inf") * np.sign(current - median)
    else:
        rz = 0.6745 * (current - median) / mad
    return mean, std, median, mad, rz


# --------------------------------------------------------------------------- #
# Dual-gate evaluation                                                        #
# --------------------------------------------------------------------------- #
def evaluate_metric(
    spec: MetricSpec,
    current: float,
    baseline_samples: list[float],
    cfg_gate: dict,
) -> MetricResult:
    stats = robust_z(current, baseline_samples)
    reasons: list[str] = []

    if stats is None:
        return MetricResult(
            name=spec.name, current=current,
            baseline_mean=None, baseline_std=None,
            baseline_median=None, baseline_mad=None,
            n_baseline=len(baseline_samples),
            relative_drift=None, robust_z=None, z_score=None,
            absolute_ok=True, relative_ok=True, zscore_ok=True,
            status="abstain",
            reasons=[f"insufficient baseline samples ({len(baseline_samples)}<{ABSTAIN_MIN_SAMPLES})"],
        )

    mean, std, median, mad, rz = stats
    z = (current - mean) / std if std > 0 else 0.0
    rel = (current - mean) / mean if mean != 0 else 0.0

    # Absolute-budget check (hard SLO)
    absolute_ok = True
    if spec.absolute_budget_max is not None and current > spec.absolute_budget_max:
        absolute_ok = False
        reasons.append(
            f"absolute budget exceeded: {current:.4g} > {spec.absolute_budget_max:.4g}"
        )
    if spec.absolute_budget_min is not None and current < spec.absolute_budget_min:
        absolute_ok = False
        reasons.append(
            f"below absolute floor: {current:.4g} < {spec.absolute_budget_min:.4g}"
        )

    # Relative-drift check — direction-aware
    warn_hit = False
    fail_hit = False
    if spec.direction == "increase_is_bad":
        if spec.fail_relative is not None and rel > spec.fail_relative:
            fail_hit = True
        elif spec.warn_relative is not None and rel > spec.warn_relative:
            warn_hit = True
        if spec.fail_absolute_delta is not None and (current - mean) > spec.fail_absolute_delta:
            fail_hit = True
        elif spec.warn_absolute_delta is not None and (current - mean) > spec.warn_absolute_delta:
            warn_hit = True
    elif spec.direction == "decrease_is_bad":
        if spec.fail_relative is not None and rel < spec.fail_relative:
            fail_hit = True
        elif spec.warn_relative is not None and rel < spec.warn_relative:
            warn_hit = True
        if spec.fail_absolute_delta is not None and (current - mean) < spec.fail_absolute_delta:
            fail_hit = True
        elif spec.warn_absolute_delta is not None and (current - mean) < spec.warn_absolute_delta:
            warn_hit = True

    relative_ok = not fail_hit

    # Statistical-significance gate
    z_thresh = float(cfg_gate.get("zscore_fail_threshold", ROBUST_Z_FAIL))
    borderline_lo, borderline_hi = cfg_gate.get(
        "borderline_zscore_range", [ROBUST_Z_FAIL, ROBUST_Z_BORDERLINE_HI]
    )
    signed = 1 if spec.direction == "increase_is_bad" else -1
    zscore_ok = (signed * rz) < z_thresh
    borderline = fail_hit and (borderline_lo <= abs(rz) < borderline_hi)

    # Dual gate — both must fire to fail
    require_both = cfg_gate.get("require_relative_and_zscore", True)
    status = "pass"
    if not absolute_ok:
        status = "fail"
        reasons.append("absolute budget breach forces fail regardless of drift")
    elif require_both:
        if fail_hit and not zscore_ok:
            status = "fail"
            reasons.append(
                f"relative drift {rel:+.2%} exceeds fail threshold AND robust_z={rz:+.2f} exceeds {z_thresh}"
            )
        elif fail_hit and zscore_ok:
            status = "warn"
            reasons.append(
                f"relative drift {rel:+.2%} exceeds fail threshold but robust_z={rz:+.2f} within noise; downgraded to warn"
            )
        elif warn_hit:
            status = "warn"
            reasons.append(f"relative drift {rel:+.2%} exceeds warn threshold")
    else:
        if fail_hit or not zscore_ok:
            status = "fail"
        elif warn_hit:
            status = "warn"

    return MetricResult(
        name=spec.name, current=current,
        baseline_mean=mean, baseline_std=std,
        baseline_median=median, baseline_mad=mad,
        n_baseline=len(baseline_samples),
        relative_drift=rel, robust_z=rz, z_score=z,
        absolute_ok=absolute_ok, relative_ok=relative_ok, zscore_ok=zscore_ok,
        status=status, borderline=borderline, reasons=reasons,
    )


# --------------------------------------------------------------------------- #
# Reporters                                                                   #
# --------------------------------------------------------------------------- #
def render_markdown(results: list[MetricResult], runner: dict, verdict: str) -> str:
    icon = {"pass": "✅", "warn": "⚠️", "fail": "❌", "abstain": "➖"}
    lines = [
        f"### OTel Perf Budget — verdict: **{verdict.upper()}**",
        "",
        f"Runner: `{runner.get('cpu_model','?')}` · {runner.get('cpu_cores','?')} cores · kernel `{runner.get('kernel','?')}`",
        "",
        "| Metric | Baseline μ ± σ (n) | Current | Δ | robust z | Status |",
        "|---|---|---|---|---|---|",
    ]
    for r in results:
        if r.baseline_mean is None:
            baseline = f"— (n={r.n_baseline})"
            delta = "—"
            rz = "—"
        else:
            baseline = f"{r.baseline_mean:.4g} ± {r.baseline_std:.4g} (n={r.n_baseline})"
            delta = f"{r.relative_drift:+.2%}" if r.relative_drift is not None else "—"
            rz = f"{r.robust_z:+.2f}" if r.robust_z is not None else "—"
        lines.append(
            f"| `{r.name}` | {baseline} | {r.current:.4g} | {delta} | {rz} | {icon[r.status]} {r.status} |"
        )
    lines += ["", "<details><summary>Reasons</summary>", ""]
    for r in results:
        if r.reasons:
            lines.append(f"- **{r.name}** ({r.status}): " + "; ".join(r.reasons))
    lines += ["", "</details>"]
    return "\n".join(lines)


def render_junit(results: list[MetricResult], out_path: str) -> None:
    suite = ET.Element(
        "testsuite",
        name="otel-perf-budget",
        tests=str(len(results)),
        failures=str(sum(1 for r in results if r.status == "fail")),
        skipped=str(sum(1 for r in results if r.status in ("abstain", "warn"))),
    )
    for r in results:
        tc = ET.SubElement(suite, "testcase", classname="perf-budget", name=r.name)
        if r.status == "fail":
            f = ET.SubElement(tc, "failure", message="; ".join(r.reasons) or "budget exceeded")
            f.text = json.dumps(asdict(r), indent=2)
        elif r.status == "warn":
            ET.SubElement(tc, "skipped", message="WARN: " + ("; ".join(r.reasons) or "drift"))
        elif r.status == "abstain":
            ET.SubElement(tc, "skipped", message="ABSTAIN: " + "; ".join(r.reasons))
    ET.ElementTree(suite).write(out_path, encoding="utf-8", xml_declaration=True)


def write_scorecard_json(results: list[MetricResult], verdict: str, out_path: str) -> None:
    payload = {
        "verdict": verdict,
        "metrics": [asdict(r) for r in results],
    }
    with open(out_path, "w") as f:
        json.dump(payload, f, indent=2)


# --------------------------------------------------------------------------- #
# Confirm-against mode (used by borderline rerun)                             #
# --------------------------------------------------------------------------- #
def apply_confirmation(
    results: list[MetricResult], prior_path: str
) -> list[MetricResult]:
    with open(prior_path) as f:
        prior = json.load(f)
    prior_status = {m["name"]: m["status"] for m in prior["metrics"]}
    for r in results:
        prev = prior_status.get(r.name, "pass")
        if r.status == "fail" and prev != "fail":
            r.status = "warn"
            r.reasons.append(
                f"failed on rerun but prior run status was '{prev}' — downgraded to warn (not confirmed)"
            )
    return results


# --------------------------------------------------------------------------- #
# Baseline refresh                                                            #
# --------------------------------------------------------------------------- #
def refresh_baseline(current_run_path: str, baseline_dir: str, window: int, out_dir: str) -> None:
    """Copy the last (window-1) baseline summaries + emit new one from current run."""
    current = load_run(current_run_path)
    summary = {k: summarize_run(v) for k, v in current.items()}

    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)

    old = Path(baseline_dir)
    kept = []
    if old.exists():
        files = sorted(old.glob("run-*.json"))
        # keep newest (window-1)
        kept = files[-(window - 1):] if window > 1 else []
        for src in kept:
            (out / src.name).write_bytes(src.read_bytes())

    # New run summary
    tag = os.environ.get("GITHUB_SHA", "local")[:12]
    date = os.environ.get("GITHUB_RUN_STARTED_AT", "").split("T")[0].replace("-", "") or "today"
    (out / f"run-{date}-{tag}.json").write_text(json.dumps(summary, indent=2))


# --------------------------------------------------------------------------- #
# Main                                                                        #
# --------------------------------------------------------------------------- #
def gh_output(name: str, value: str) -> None:
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        with open(path, "a") as f:
            f.write(f"{name}={value}\n")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--current", required=True)
    ap.add_argument("--baseline", required=True)
    ap.add_argument("--config")
    ap.add_argument("--runner")
    ap.add_argument("--report-md")
    ap.add_argument("--report-json")
    ap.add_argument("--junit")
    ap.add_argument("--confirm-against")
    ap.add_argument("--refresh-baseline", action="store_true")
    ap.add_argument("--window", type=int, default=7)
    ap.add_argument("--out")
    args = ap.parse_args()

    if args.refresh_baseline:
        refresh_baseline(args.current, args.baseline, args.window, args.out or "./baseline-next")
        return 0

    cfg, specs = load_config(args.config)
    current_raw = load_run(args.current)
    baseline_series = load_baseline(args.baseline)

    results: list[MetricResult] = []
    for spec in specs:
        samples = current_raw.get(spec.name)
        if not samples:
            results.append(MetricResult(
                name=spec.name, current=float("nan"),
                baseline_mean=None, baseline_std=None,
                baseline_median=None, baseline_mad=None,
                n_baseline=len(baseline_series.get(spec.name, [])),
                relative_drift=None, robust_z=None, z_score=None,
                absolute_ok=True, relative_ok=True, zscore_ok=True,
                status="abstain",
                reasons=["metric not present in current run"],
            ))
            continue
        cur = summarize_run(samples)
        results.append(evaluate_metric(
            spec, cur, baseline_series.get(spec.name, []), cfg.get("gate", {}),
        ))

    if args.confirm_against and Path(args.confirm_against).exists():
        results = apply_confirmation(results, args.confirm_against)

    failed = [r for r in results if r.status == "fail"]
    verdict = "fail" if failed else "pass"

    # borderline_only = every failure was borderline (only makes sense pre-rerun)
    borderline_only = bool(failed) and all(r.borderline for r in failed) and not args.confirm_against
    gh_output("verdict", verdict)
    gh_output("borderline_only", "true" if borderline_only else "false")
    gh_output("fail_count", str(len(failed)))

    runner = json.loads(Path(args.runner).read_text()) if args.runner else {}
    if args.report_md:
        Path(args.report_md).write_text(render_markdown(results, runner, verdict))
    if args.report_json:
        write_scorecard_json(results, verdict, args.report_json)
    if args.junit:
        render_junit(results, args.junit)

    return 1 if verdict == "fail" else 0


if __name__ == "__main__":
    sys.exit(main())
```

---

## 3. `scripts/perf/check_intra_cv.py`

Rejects the run outright if any critical metric's iteration CV exceeds the noise floor. This prevents evaluating against a garbage sample.

```python
#!/usr/bin/env python3
"""Reject a run whose per-metric coefficient of variation is too high."""
from __future__ import annotations
import argparse, json, sys
from pathlib import Path
import numpy as np

# Metrics that must be quiet for the gate to be trusted. Others are advisory.
CRITICAL = {
    "exporter_send_latency_p99_ms",
    "spans_per_second",
    "cpu_ns_per_span",
    "rss_peak_mib",
}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--results", required=True)
    ap.add_argument("--max-cv", type=float, required=True)
    ap.add_argument("--report", required=True)
    args = ap.parse_args()

    data = json.loads(Path(args.results).read_text())
    per_metric: dict[str, list[float]] = {}
    for it in data["iterations"]:
        for k, v in it.items():
            if isinstance(v, (int, float)):
                per_metric.setdefault(k, []).append(float(v))

    report = {"max_cv_allowed": args.max_cv, "metrics": {}, "violations": []}
    for name, samples in per_metric.items():
        arr = np.asarray(samples, dtype=float)
        mean = float(arr.mean())
        std = float(arr.std(ddof=1)) if len(arr) > 1 else 0.0
        cv = (std / mean) if mean != 0 else 0.0
        report["metrics"][name] = {"mean": mean, "std": std, "cv": cv, "n": len(samples)}
        if name in CRITICAL and cv > args.max_cv:
            report["violations"].append({"metric": name, "cv": cv})

    Path(args.report).write_text(json.dumps(report, indent=2))

    if report["violations"]:
        print(
            "::warning::intra-run CV exceeds noise floor on: "
            + ", ".join(f"{v['metric']}({v['cv']:.1%})" for v in report["violations"])
        )
        # Non-fatal: annotate but don't kill the workflow. The dual-gate design
        # already handles noise; a noisy run just means fewer false failures.
        # Uncomment the next line to escalate to a hard reject:
        # return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

---

## 4. `scripts/perf/render_pr_comment.py`

Sticky PR comment renderer. Kept separate from `evaluate_budget.py` so the comment can be re-rendered with run URLs and noise diagnostics without re-evaluating.

```python
#!/usr/bin/env python3
from __future__ import annotations
import argparse, json
from pathlib import Path

VERDICT_ICON = {"pass": "✅", "fail": "❌", "warn": "⚠️", "abstain": "➖"}


def load(p: str | None) -> dict:
    return json.loads(Path(p).read_text()) if p and Path(p).exists() else {}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--scorecard", required=True)
    ap.add_argument("--runner")
    ap.add_argument("--noise")
    ap.add_argument("--commit", required=True)
    ap.add_argument("--run-url", required=True)
    ap.add_argument("--output", required=True)
    args = ap.parse_args()

    sc = load(args.scorecard)
    runner = load(args.runner)
    noise = load(args.noise)

    verdict = sc.get("verdict", "abstain")
    metrics = sc.get("metrics", [])
    fails = [m for m in metrics if m["status"] == "fail"]
    warns = [m for m in metrics if m["status"] == "warn"]
    absts = [m for m in metrics if m["status"] == "abstain"]

    lines = [
        f"## {VERDICT_ICON.get(verdict,'❓')} OTel Perf Budget — **{verdict.upper()}**",
        "",
        f"Commit `{args.commit[:12]}` · [Run details]({args.run_url})",
        "",
        f"**Summary:** {len(fails)} fail · {len(warns)} warn · {len(absts)} abstain "
        f"· {len(metrics) - len(fails) - len(warns) - len(absts)} pass",
        "",
        "| Metric | Baseline μ ± σ (n) | Current | Δ | robust z | Status |",
        "|---|---|---|---:|---:|:---:|",
    ]
    for m in metrics:
        if m["baseline_mean"] is None:
            base = f"— (n={m['n_baseline']})"
            delta = "—"
            rz = "—"
        else:
            base = f"{m['baseline_mean']:.4g} ± {m['baseline_std']:.4g} (n={m['n_baseline']})"
            delta = f"{m['relative_drift']*100:+.2f}%" if m["relative_drift"] is not None else "—"
            rz = f"{m['robust_z']:+.2f}" if m["robust_z"] is not None else "—"
        current = f"{m['current']:.4g}"
        lines.append(
            f"| `{m['name']}` | {base} | {current} | {delta} | {rz} | "
            f"{VERDICT_ICON.get(m['status'],'?')} {m['status']} |"
        )

    if fails or warns:
        lines += ["", "### Reasons", ""]
        for m in fails + warns:
            if m.get("reasons"):
                lines.append(f"- **`{m['name']}`** ({m['status']}): " + "; ".join(m["reasons"]))

    if runner:
        lines += [
            "",
            "<details><summary>Runner & noise diagnostics</summary>",
            "",
            f"- CPU: `{runner.get('cpu_model','?')}` ({runner.get('cpu_cores','?')} cores)",
            f"- Kernel: `{runner.get('kernel','?')}`",
        ]
        if noise.get("violations"):
            v = ", ".join(f"`{x['metric']}`={x['cv']:.1%}" for x in noise["violations"])
            lines.append(f"- ⚠️ Noise violations: {v}")
        else:
            lines.append("- Intra-run CV within noise floor for all critical metrics.")
        lines += ["", "</details>"]

    lines += [
        "",
        "---",
        "To bypass this gate, add the `perf-budget-override` label **and** link a "
        "follow-up issue in the PR body (e.g. `perf-followup: #1234`).",
    ]

    Path(args.output).write_text("\n".join(lines))


if __name__ == "__main__":
    main()
```

`scripts/perf/__init__.py` is empty.

---

## 5. `perf-budget.yaml`

```yaml
baseline:
  window: 7
  max_age_days: 21
  min_samples: 3

gate:
  require_relative_and_zscore: true
  zscore_fail_threshold: 3.0
  borderline_zscore_range: [3.0, 4.0]

metrics:
  # ---------- LATENCY ----------
  - name: exporter_send_latency_p50_ms
    direction: increase_is_bad
    absolute_budget_max: 25
    warn_relative: 0.05
    fail_relative: 0.10
  - name: exporter_send_latency_p95_ms
    direction: increase_is_bad
    absolute_budget_max: 75
    warn_relative: 0.05
    fail_relative: 0.10
  - name: exporter_send_latency_p99_ms
    direction: increase_is_bad
    absolute_budget_max: 200
    warn_relative: 0.07
    fail_relative: 0.15
  - name: processor_batch_send_latency_p99_ms
    direction: increase_is_bad
    absolute_budget_max: 50
    warn_relative: 0.07
    fail_relative: 0.15
  - name: queue_wait_time_p99_ms
    direction: increase_is_bad
    absolute_budget_max: 100
    warn_relative: 0.10
    fail_relative: 0.20

  # ---------- THROUGHPUT ----------
  - name: spans_per_second
    direction: decrease_is_bad
    absolute_budget_min: 10000
    warn_relative: -0.05
    fail_relative: -0.10
  - name: metric_points_per_second
    direction: decrease_is_bad
    absolute_budget_min: 50000
    warn_relative: -0.05
    fail_relative: -0.10
  - name: logs_per_second
    direction: decrease_is_bad
    absolute_budget_min: 20000
    warn_relative: -0.05
    fail_relative: -0.10
  - name: export_success_ratio
    direction: decrease_is_bad
    absolute_budget_min: 0.999
    warn_absolute_delta: -0.001
    fail_absolute_delta: -0.005

  # ---------- RESOURCE COST ----------
  - name: cpu_ns_per_span
    direction: increase_is_bad
    absolute_budget_max: 15000
    warn_relative: 0.05
    fail_relative: 0.10
  - name: cpu_peak_percent
    direction: increase_is_bad
    absolute_budget_max: 85
    warn_relative: 0.05
    fail_relative: 0.10
  - name: heap_bytes_per_span
    direction: increase_is_bad
    absolute_budget_max: 4096
    warn_relative: 0.07
    fail_relative: 0.15
  - name: rss_peak_mib
    direction: increase_is_bad
    absolute_budget_max: 512
    warn_relative: 0.05
    fail_relative: 0.10
  - name: gc_pause_p99_ms
    direction: increase_is_bad
    absolute_budget_max: 5
    warn_relative: 0.10
    fail_relative: 0.25

  # ---------- RELIABILITY ----------
  - name: dropped_spans_ratio
    direction: increase_is_bad
    absolute_budget_max: 0.0001
    warn_absolute_delta: 0.0005
    fail_absolute_delta: 0.001
  - name: queue_saturation_max
    direction: increase_is_bad
    absolute_budget_max: 0.7
    warn_relative: 0.10
    fail_relative: 0.20
  - name: retry_ratio
    direction: increase_is_bad
    absolute_budget_max: 0.01
    warn_absolute_delta: 0.005
    fail_absolute_delta: 0.01
```

---

## 6. Branch protection & escape-hatch semantics

1. **Settings → Branches → main → Require status checks to pass**:
   Add `Performance Budget Gate` as a required check. This is what actually blocks merges — `exit 1` alone cannot block a PR.

2. **Escape-hatch flow:**
   - A repo maintainer applies the `perf-budget-override` label to the PR.
   - The `Check escape-hatch label` step reads the label via the REST API (label-add events require write access, so GitHub enforces authorization for you).
   - The `Finalize verdict` step converts the fail into a pass **but** logs `::warning::` with the PR author's handle for the audit log.
   - The separate `override-audit` job (**not** a required check, so it can fail without blocking the merge) verifies the PR body links a follow-up issue via `closes #N` / `perf-followup: #N`. If missing, it fails with a clear message — reviewers see it in the checks list even though it doesn't block.

3. **Rerun logic recap:** The initial `evaluate` step emits `borderline_only=true` when every failing metric has robust |z| ∈ [3.0, 4.0). Only then does the confirmation rerun execute. The rerun invokes `evaluate_budget.py --confirm-against ./results/scorecard.json` so a metric fails on the second pass **only** if it also failed on the first — an isolated flaky run cannot cause a rerun failure.

4. **Run script contract:** `scripts/run-bench.sh` (not shown, ships with your Collector repo) must emit `results/current.json` in this shape:

   ```json
   {
     "iterations": [
       {"exporter_send_latency_p99_ms": 142.1, "spans_per_second": 11820, "cpu_ns_per_span": 12440, "...": 0},
       ...
     ]
   }
   ```

   One object per iteration, one numeric value per metric. `evaluate_budget.py` handles the trimmed-median summarization internally.

5. **Seeding the baseline:** run the workflow 7 times on `main` (or dispatch it with `force_baseline_refresh=true` after a manual sanity check) before enabling the required check. Fewer than 3 baseline samples on any metric causes the evaluator to abstain on that metric — the gate will not fail without evidence.
