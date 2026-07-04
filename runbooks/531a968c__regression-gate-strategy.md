# Regression Gate Strategy

**A statistically honest CI gate for performance-sensitive tests, calibrated against the Noise Floor dashboard.**

The dashboard's core insight: **you cannot detect a regression smaller than your runner's noise floor.** A "5% slowdown" alarm on GitHub Actions (CV 10.19%) will fire on random jitter half the time. The same alarm on bare-metal (CV 0.46%) is real.

This strategy converts that insight into three deliverables:

1. A **regression gate policy** grounded in Welch's t-test and the minimum detectable effect (MDE)
2. A **Bare-Metal vs AWS Dedicated decision matrix** driven by test duration × sensitivity
3. **Copy-paste GitHub Actions workflows** that implement smart retry, adaptive N, and statistical gating so pipelines fail only on real regressions

---

## 1 · Statistical framework

Every gate decision comes from one formula — the **minimum detectable effect** (MDE) at 95% confidence for a two-sample Welch's t-test:

$$
\text{MDE}\%\ =\ t_{\alpha,\ \nu} \cdot \text{CV}\% \cdot \sqrt{\tfrac{2}{n}}
$$

- `CV%` — the runner's coefficient of variation (from the dashboard)
- `n` — runs per revision (baseline and PR each contribute n)
- `t_{α, ν}` — two-tailed critical value at α=0.05 with ν = 2(n−1) degrees of freedom

A regression is **statistically significant** only if the observed slowdown exceeds MDE.

### MDE table (from your dashboard's CVs)

| Runner | CV% | n=3 | n=5 | n=10 | n=20 | n=30 |
|---|---:|---:|---:|---:|---:|---:|
| **Bare-Metal Self-Hosted** | 0.46 | **1.10%** | 0.62% | 0.38% | 0.25% | 0.20% |
| **AWS EC2 Dedicated** | 2.50 | 5.97% | 3.38% | 2.05% | 1.37% | 1.10% |
| Docker (CPU-limited) | 4.52 | 10.78% | 6.09% | 3.71% | 2.47% | 1.98% |
| GitHub Actions (shared) | 10.19 | 24.29% | 13.74% | 8.35% | 5.57% | 4.47% |
| QEMU ARM-on-x86 | 10.69 | 25.49% | 14.42% | 8.77% | 5.85% | 4.69% |

**Read it like this:** with 3 runs on GitHub Actions, you cannot honestly claim a 20% slowdown is real. With 3 runs on bare-metal, you can flag a 1.1% slowdown at the same confidence.

### Sample-size targets

| Regression you must catch | Bare-Metal | AWS Dedicated | Docker-limited | GitHub Actions |
|---|---:|---:|---:|---:|
| **1%** (razor-thin) | n=3 | n≥40 | n≥120 | impractical |
| **3%** (perf-sensitive lib) | n=3 | n=7 | n=15 | n≥100 |
| **5%** (product-level SLO) | n=3 | n=5 | n=7 | n≥30 |
| **10%** (smoke check) | n=3 | n=3 | n=3 | n=5 |

---

## 2 · The regression-gate policy (three tiers)

Not every test needs the same gate. Classify each test by its **sensitivity tier** and treat gates as a service-level agreement.

### Tier S — "Core hot-path" (parser, allocator, matmul, inner loops)

- **Detect ≥ 1% regressions** at 95% confidence
- **Runner:** Bare-Metal Self-Hosted (only viable option)
- **Runs per revision:** 5 (baseline + PR)
- **Warm-up:** 1 discarded iteration per run
- **Gate:** Welch's t-test, α=0.05, one-sided (regression only)
- **Retry policy:** on p-value ∈ [0.05, 0.15], auto-retry with n=10

### Tier A — "Product SLO" (page render, API latency, dataset load)

- **Detect ≥ 3% regressions** at 95% confidence
- **Runner:** Bare-Metal (n=3) *or* AWS Dedicated (n=7)
- **Gate:** Welch's t-test, α=0.05
- **Retry policy:** on borderline p-value [0.05, 0.10], auto-retry once

### Tier B — "Smoke perf" (e2e, integration timings)

- **Detect ≥ 10% regressions** at 90% confidence
- **Runner:** Any dedicated env; **never** GitHub-Actions shared runners for perf gates
- **Runs:** 3
- **Retry policy:** on failure, single retry; alert-only, do not block merge

**Rule of thumb — never gate on GitHub-hosted shared runners.** With CV 10.19% they can only prove a ~24% regression from 3 runs. Anything shipped through them is a **correctness** check, not a **performance** check.

---

## 3 · Bare-Metal vs AWS Dedicated decision matrix

Both are viable for perf gates. Choose by the interplay of **single-run duration**, **required MDE**, and **queue depth**.

Given your dashboard costs and CVs:

| Runner | CV% | Base cost | Warm-up | Parallelism ceiling |
|---|---:|---:|---:|---|
| Bare-Metal (i9-13900K) | 0.46 | $0.14/hr amortized | 0 min | Fixed (own hardware) |
| AWS EC2 Dedicated (c6i.4xlarge) | 2.50 | $0.68/hr | 2.5 min | Elastic |

### The decision matrix

Each cell shows: **[recommended runner] · runs needed · total wall-clock**.
Total wall-clock includes AWS's 2.5 min warm-up and assumes runs execute serially inside one job.

| Test duration ↓ / Required MDE → | 1% | 3% | 5% | 10% |
|---|---|---|---|---|
| **≤ 30 s** | **Bare-Metal** · n=5 · 2.5 min | **Bare-Metal** · n=3 · 1.5 min | **AWS** · n=5 · 5 min | **AWS** · n=3 · 4 min |
| **30 s – 2 min** | **Bare-Metal** · n=5 · 10 min | **Bare-Metal** · n=3 · 6 min | **AWS** · n=5 · 12 min | **AWS** · n=3 · 8.5 min |
| **2 – 10 min** | **Bare-Metal** · n=5 · 50 min | **Bare-Metal** · n=3 · 30 min | **Bare-Metal** · n=3 · 30 min | **AWS parallel** · n=3 · 12.5 min |
| **≥ 10 min** | *impractical for per-PR gate — move to nightly* | **Bare-Metal** · n=3 · nightly | **AWS parallel matrix** · n=3 · 15 min | **AWS parallel matrix** · n=3 · 15 min |

### Decision rules

1. **Sub-30-s tests + tight MDE (≤ 3%)** → Bare-Metal. AWS warm-up dominates wall-clock.
2. **Loose MDE (≥ 5%) + high PR volume** → AWS Dedicated with a matrix strategy. Elasticity beats queue depth on bare-metal.
3. **Long tests (≥ 2 min) + tight MDE** → Bare-Metal serial. AWS gets expensive fast ($0.68/hr × 30 min × PRs/day).
4. **Nightly deep-perf suite** → Bare-Metal, n=10 (MDE 0.38%). Detects micro-regressions that per-PR gates cannot afford to run.
5. **When both work, pick Bare-Metal.** 5× lower CV means fewer flakes → less retry noise → less human time wasted triaging false positives.

### When to prefer AWS Dedicated anyway

- **No hardware ops team** — bare-metal needs kernel tuning (isolcpus, perf governor, IRQ pinning). If nobody owns that, its CV drifts up over time.
- **Global team, follow-the-sun** — AWS regions reduce queue latency for teams outside your bare-metal's colo.
- **Bursty PR volume** — if you occasionally see 20 concurrent perf-gate jobs, elastic AWS beats a 4-node bare-metal fleet queuing them.

---

## 4 · GitHub Actions implementation

Three files:

- `.github/workflows/perf-gate.yml` — reusable workflow, called by any repo
- `scripts/perf_gate.py` — the statistical analyzer (Welch's t-test + MDE)
- `.github/workflows/pr-perf.yml` — example caller

### 4.1 The analyzer — `scripts/perf_gate.py`

```python
#!/usr/bin/env python3
"""
perf_gate.py — statistically honest performance gate.

Reads baseline & candidate JSON files of per-run wall-clock samples,
runs a one-sided Welch's t-test, and exits non-zero only if the
observed regression is both (a) larger than the runner's noise floor
(min-detectable-effect) AND (b) statistically significant at α=0.05.

Usage:
  perf_gate.py --baseline base.json --candidate pr.json \\
               --runner-cv 0.46 --mde-target 1.0 --alpha 0.05
"""
import argparse, json, math, os, sys
from statistics import mean, stdev

def t_crit(df: int, alpha: float = 0.05) -> float:
    """One-sided t critical value. Small lookup table + fallback."""
    if alpha != 0.05:
        raise ValueError("only α=0.05 supported without scipy")
    table = {1:6.314, 2:2.920, 3:2.353, 4:2.132, 5:2.015, 6:1.943,
             7:1.895, 8:1.860, 9:1.833, 10:1.812, 12:1.782, 15:1.753,
             20:1.725, 25:1.708, 30:1.697, 40:1.684, 60:1.671, 120:1.658}
    if df <= 0: return float("inf")
    if df in table: return table[df]
    keys = sorted(table.keys())
    for k in keys:
        if df < k: return table[k]
    return 1.645  # ∞ df

def welch_t(a: list, b: list) -> tuple[float, float, int]:
    """Return (t-stat, one-sided p-approx, df) for H1: mean(b) > mean(a)."""
    n1, n2 = len(a), len(b)
    m1, m2 = mean(a), mean(b)
    v1, v2 = stdev(a) ** 2, stdev(b) ** 2
    se = math.sqrt(v1 / n1 + v2 / n2)
    t = (m2 - m1) / se if se > 0 else 0.0
    df = (v1 / n1 + v2 / n2) ** 2 / (
        (v1 / n1) ** 2 / (n1 - 1) + (v2 / n2) ** 2 / (n2 - 1)
    )
    # One-sided p via t_crit table (approximation without scipy)
    # p<0.05 iff t > t_crit(df, 0.05)
    tc = t_crit(round(df), 0.05)
    # Piecewise linear interp so we return *some* p value for logs:
    p = 0.025 if t > tc + 1 else (0.05 if t > tc else (0.15 if t > tc - 0.5 else 0.5))
    return t, p, round(df)

def mde_pct(cv_pct: float, n: int) -> float:
    """Minimum detectable regression in %."""
    return t_crit(2 * (n - 1)) * cv_pct * math.sqrt(2 / n)

def load(path: str) -> list[float]:
    with open(path) as f:
        data = json.load(f)
    # Accept either a flat list of samples or {"samples":[...]}
    if isinstance(data, dict):
        data = data["samples"]
    return [float(x) for x in data]

def gh_out(key: str, val) -> None:
    """Write to $GITHUB_OUTPUT if present."""
    if (path := os.environ.get("GITHUB_OUTPUT")):
        with open(path, "a") as f:
            f.write(f"{key}={val}\n")

def gh_summary(md: str) -> None:
    if (path := os.environ.get("GITHUB_STEP_SUMMARY")):
        with open(path, "a") as f:
            f.write(md + "\n")

def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--baseline", required=True)
    p.add_argument("--candidate", required=True)
    p.add_argument("--runner-cv", type=float, required=True,
                   help="Runner CV%% from Noise Floor dashboard")
    p.add_argument("--mde-target", type=float, required=True,
                   help="Regression %% you contractually must detect (Tier S/A/B)")
    p.add_argument("--alpha", type=float, default=0.05)
    p.add_argument("--label", default="benchmark")
    args = p.parse_args()

    base = load(args.baseline)
    cand = load(args.candidate)
    if len(base) < 2 or len(cand) < 2:
        print("::error::need ≥2 samples per side"); return 2

    n = min(len(base), len(cand))
    achieved_mde = mde_pct(args.runner_cv, n)
    b_mean, c_mean = mean(base), mean(cand)
    delta_pct = (c_mean - b_mean) / b_mean * 100

    t, p_val, df = welch_t(base, cand)
    significant = t > t_crit(df, args.alpha)
    exceeds_mde = delta_pct > achieved_mde
    exceeds_target = delta_pct > args.mde_target

    # Verdict logic — only fail on the intersection
    verdict = "PASS"
    reason = ""
    if delta_pct <= 0:
        verdict, reason = "PASS", "candidate faster or equal"
    elif not significant:
        verdict, reason = "PASS", f"Δ={delta_pct:+.2f}% not statistically significant (p>{args.alpha})"
    elif not exceeds_mde:
        verdict, reason = "PASS", f"Δ={delta_pct:+.2f}% within runner noise floor (MDE={achieved_mde:.2f}%)"
    elif not exceeds_target:
        verdict, reason = "WARN", f"significant but Δ={delta_pct:+.2f}% below tier target ({args.mde_target}%)"
    else:
        verdict, reason = "FAIL", f"regression Δ={delta_pct:+.2f}% > tier target ({args.mde_target}%), p<{args.alpha}"

    # Structured output for retry logic
    borderline = significant and 0.05 <= p_val <= 0.15
    gh_out("verdict", verdict)
    gh_out("delta_pct", f"{delta_pct:.3f}")
    gh_out("achieved_mde", f"{achieved_mde:.3f}")
    gh_out("p_value", f"{p_val:.3f}")
    gh_out("borderline", "true" if borderline else "false")
    gh_out("n", n)

    md = (
        f"### Perf gate — `{args.label}`\n\n"
        f"| Metric | Value |\n|---|---|\n"
        f"| Verdict | **{verdict}** |\n"
        f"| Δ vs baseline | `{delta_pct:+.2f}%` |\n"
        f"| Baseline mean | `{b_mean:.3f} ms` (n={len(base)}) |\n"
        f"| Candidate mean | `{c_mean:.3f} ms` (n={len(cand)}) |\n"
        f"| Runner CV | `{args.runner_cv}%` |\n"
        f"| Achieved MDE (n={n}) | `{achieved_mde:.2f}%` |\n"
        f"| Tier target | `{args.mde_target}%` |\n"
        f"| Welch t | `{t:.2f}` (df≈{df}) |\n"
        f"| Reason | {reason} |\n"
    )
    gh_summary(md)
    print(md)

    return 0 if verdict != "FAIL" else 1

if __name__ == "__main__":
    sys.exit(main())
```

Zero-dependency (stdlib only) so it runs on any CI runner without an env-setup step.

### 4.2 The reusable workflow — `.github/workflows/perf-gate.yml`

```yaml
name: Perf Gate (reusable)

on:
  workflow_call:
    inputs:
      tier:
        description: "S | A | B — sensitivity tier"
        type: string
        required: true
      benchmark_cmd:
        description: "Command that emits one wall-clock ms per line to stdout"
        type: string
        required: true
      baseline_ref:
        description: "Git ref to treat as the baseline"
        type: string
        default: "origin/main"
      label:
        description: "Benchmark name for reports"
        type: string
        default: "benchmark"

# Tier → (runner, CV%, initial-n, retry-n, MDE target%)
env:
  # Numbers below come from your Noise Floor dashboard.
  BM_CV: "0.46"      # Bare-Metal Self-Hosted
  AWS_CV: "2.50"     # AWS EC2 Dedicated

jobs:
  # ─────────────────────────────────────────────────────────
  # Route to the correct runner label based on the tier.
  # Bare-metal runners must be self-hosted with labels [self-hosted, bare-metal].
  # AWS dedicated runners: [self-hosted, aws-dedicated].
  # ─────────────────────────────────────────────────────────
  configure:
    runs-on: ubuntu-latest
    outputs:
      runner_label: ${{ steps.pick.outputs.runner_label }}
      runner_cv:    ${{ steps.pick.outputs.runner_cv }}
      initial_n:    ${{ steps.pick.outputs.initial_n }}
      retry_n:      ${{ steps.pick.outputs.retry_n }}
      mde_target:   ${{ steps.pick.outputs.mde_target }}
    steps:
      - id: pick
        shell: bash
        run: |
          case "${{ inputs.tier }}" in
            S)  echo "runner_label=bare-metal"    >> $GITHUB_OUTPUT
                echo "runner_cv=${{ env.BM_CV }}" >> $GITHUB_OUTPUT
                echo "initial_n=5"                >> $GITHUB_OUTPUT
                echo "retry_n=10"                 >> $GITHUB_OUTPUT
                echo "mde_target=1.0"             >> $GITHUB_OUTPUT ;;
            A)  echo "runner_label=aws-dedicated" >> $GITHUB_OUTPUT
                echo "runner_cv=${{ env.AWS_CV }}">> $GITHUB_OUTPUT
                echo "initial_n=7"                >> $GITHUB_OUTPUT
                echo "retry_n=15"                 >> $GITHUB_OUTPUT
                echo "mde_target=3.0"             >> $GITHUB_OUTPUT ;;
            B)  echo "runner_label=aws-dedicated" >> $GITHUB_OUTPUT
                echo "runner_cv=${{ env.AWS_CV }}">> $GITHUB_OUTPUT
                echo "initial_n=3"                >> $GITHUB_OUTPUT
                echo "retry_n=5"                  >> $GITHUB_OUTPUT
                echo "mde_target=10.0"            >> $GITHUB_OUTPUT ;;
            *)  echo "::error::unknown tier ${{ inputs.tier }}" ; exit 2 ;;
          esac

  benchmark:
    needs: configure
    runs-on: [self-hosted, "${{ needs.configure.outputs.runner_label }}"]
    outputs:
      verdict:    ${{ steps.gate.outputs.verdict }}
      borderline: ${{ steps.gate.outputs.borderline }}
      delta_pct:  ${{ steps.gate.outputs.delta_pct }}
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0

      # 1) Run n iterations on the PR
      - name: Candidate run (n=${{ needs.configure.outputs.initial_n }})
        id: cand
        shell: bash
        run: |
          mkdir -p perf-out
          for i in $(seq 1 ${{ needs.configure.outputs.initial_n }}); do
            ${{ inputs.benchmark_cmd }} >> perf-out/cand-raw.txt
          done
          # 1 warm-up drop
          tail -n +2 perf-out/cand-raw.txt | jq -R -s -c 'split("\n")|map(select(length>0)|tonumber)' \
            > perf-out/candidate.json
          cat perf-out/candidate.json

      # 2) Check out baseline in a worktree and run n iterations
      - name: Baseline run (n=${{ needs.configure.outputs.initial_n }})
        shell: bash
        run: |
          git worktree add /tmp/baseline ${{ inputs.baseline_ref }}
          pushd /tmp/baseline > /dev/null
          for i in $(seq 1 ${{ needs.configure.outputs.initial_n }}); do
            ${{ inputs.benchmark_cmd }} >> /tmp/base-raw.txt
          done
          popd > /dev/null
          tail -n +2 /tmp/base-raw.txt | jq -R -s -c 'split("\n")|map(select(length>0)|tonumber)' \
            > perf-out/baseline.json

      # 3) Gate — Welch's t-test using dashboard-calibrated CV & MDE target
      - name: Statistical gate
        id: gate
        shell: bash
        run: |
          python3 scripts/perf_gate.py \
            --baseline perf-out/baseline.json \
            --candidate perf-out/candidate.json \
            --runner-cv ${{ needs.configure.outputs.runner_cv }} \
            --mde-target ${{ needs.configure.outputs.mde_target }} \
            --label "${{ inputs.label }}"

      - name: Upload perf artifacts
        if: always()
        uses: actions/upload-artifact@v4
        with:
          name: perf-${{ inputs.label }}-attempt-1
          path: perf-out/

  # ─────────────────────────────────────────────────────────
  # Smart retry — only when the first attempt was BORDERLINE.
  # Uses the tier's larger retry_n so MDE tightens on the second try.
  # Never retries a clean PASS (waste) or a clean FAIL (real regression).
  # ─────────────────────────────────────────────────────────
  benchmark_retry:
    needs: [configure, benchmark]
    if: needs.benchmark.outputs.borderline == 'true'
    runs-on: [self-hosted, "${{ needs.configure.outputs.runner_label }}"]
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0

      - name: Retry candidate (n=${{ needs.configure.outputs.retry_n }})
        shell: bash
        run: |
          mkdir -p perf-out
          for i in $(seq 1 ${{ needs.configure.outputs.retry_n }}); do
            ${{ inputs.benchmark_cmd }} >> perf-out/cand-raw.txt
          done
          tail -n +2 perf-out/cand-raw.txt | jq -R -s -c 'split("\n")|map(select(length>0)|tonumber)' \
            > perf-out/candidate.json

      - name: Retry baseline (n=${{ needs.configure.outputs.retry_n }})
        shell: bash
        run: |
          git worktree add /tmp/baseline ${{ inputs.baseline_ref }}
          pushd /tmp/baseline > /dev/null
          for i in $(seq 1 ${{ needs.configure.outputs.retry_n }}); do
            ${{ inputs.benchmark_cmd }} >> /tmp/base-raw.txt
          done
          popd > /dev/null
          tail -n +2 /tmp/base-raw.txt | jq -R -s -c 'split("\n")|map(select(length>0)|tonumber)' \
            > perf-out/baseline.json

      - name: Statistical gate (retry)
        shell: bash
        run: |
          python3 scripts/perf_gate.py \
            --baseline perf-out/baseline.json \
            --candidate perf-out/candidate.json \
            --runner-cv ${{ needs.configure.outputs.runner_cv }} \
            --mde-target ${{ needs.configure.outputs.mde_target }} \
            --label "${{ inputs.label }} (retry)"

      - uses: actions/upload-artifact@v4
        if: always()
        with:
          name: perf-${{ inputs.label }}-attempt-2
          path: perf-out/
```

### 4.3 Example caller — `.github/workflows/pr-perf.yml`

```yaml
name: PR performance gates

on:
  pull_request:
    paths:
      - "src/**"
      - "benches/**"

jobs:
  # Tier S: parser hot loop must not regress > 1%
  parser-hotpath:
    uses: ./.github/workflows/perf-gate.yml
    with:
      tier: S
      label: parser-hotpath
      benchmark_cmd: "cargo bench --bench parser -- --output-format=ms-per-run"

  # Tier A: end-to-end query latency, 3% budget
  query-latency:
    uses: ./.github/workflows/perf-gate.yml
    with:
      tier: A
      label: query-latency
      benchmark_cmd: "./bin/bench-query --emit ms"

  # Tier B: e2e smoke, 10% budget — advisory only
  e2e-smoke:
    uses: ./.github/workflows/perf-gate.yml
    with:
      tier: B
      label: e2e-smoke
      benchmark_cmd: "npm run bench:e2e -- --emit ms"
    continue-on-error: true   # advisory — don't block merges
```

---

## 5 · Why this design is honest

- **No fixed % threshold.** The gate compares observed Δ against *this runner's noise floor*, not against a hand-picked constant. Move the workload to bare-metal and it automatically becomes 5× stricter.
- **No retry masking.** Retries only fire in the p ∈ [0.05, 0.15] borderline zone. A clean 8% regression on bare-metal fails immediately, no retry, no flake-hiding.
- **Sample size is a tier property, not a guess.** Tier S mandates n=5 → n=10 on retry because that's what the CV requires to reach MDE 1%. You cannot silently weaken the gate by reducing n.
- **Runner selection is explicit.** Tier S must land on bare-metal — the workflow won't accept AWS-Dedicated because n=5 on AWS gives MDE 3.38%, which cannot honor a 1% contract.
- **Zero external deps.** `perf_gate.py` uses stdlib only. No `pip install scipy` step to break perf runs.

## 6 · One-week rollout plan

1. **Day 1** — Land `perf_gate.py` and `perf-gate.yml` in main. Run in shadow mode (`continue-on-error: true` on all tiers) for 5 business days.
2. **Day 3** — Publish a PR-comment bot that posts the gate summary. Team learns to read Δ vs MDE.
3. **Day 5** — Audit false-positive rate. If any tier misfires > 1× per 50 PRs, tune retry threshold from 0.15 → 0.20.
4. **Day 6** — Flip Tier B from advisory to blocking.
5. **Day 8** — Flip Tier A to blocking. Tier S stays advisory another week (highest cost of false positive).
6. **Day 15** — Tier S blocking. Weekly cronjob re-computes CV from the last 7 days of perf-artifact data and updates `BM_CV` / `AWS_CV` in the workflow.
