# Canary Deployment Playbook — atomic.Pointer Exporter Refactor

**Goal.** Roll out the `atomic.Pointer[T]` router refactor from [SOP §2.2](./otel_exporter_concurrency_sop.md) to a single canary node in a 50-pod OTel Collector fleet, routing **2% of traffic**, monitoring canary vs control side-by-side, with a **Slack-triggered rollback** available for the first hour.

**Position in the series.**

1. [SOP](./otel_exporter_concurrency_sop.md) — how to write it
2. [Contrib comparison](./otel_exporter_concurrency_comparison.md) — what's already deployed
3. [Local microbenchmark](./lbbench_analysis.md) — 1.7× / 40% p99 lower bound
4. [64-core load test plan](./lbloadtest_plan.md) — validates lower bound at production core count
5. **This document** — validates it under real production traffic on 2% of the fleet

The prior four artifacts run in labs. This one runs in production. Everything from the load-test plan carries forward — including the 1.7× lower bound as the pass/fail anchor.

---

## 1. Rollout timeline

The rollout is **six phases over 8 hours** on day one, followed by a graduated ramp on days 2–5:

| Phase | Duration | Traffic | Action | Gate |
|---|---|---|---|---|
| 0. Pre-flight | 15 min | 0% | Deploy PrometheusRule + Slack bot; verify metrics scrape | All 8 alert rules load without syntax error; `/healthz` returns 200 |
| 1. Ignition | 5 min | 2% (1/50 pods) | `kubectl apply -f k8s/collector-daemonset.yaml` (canary replicas: 1, baseline: 49) | Canary pod `Ready` within 60s; no `CrashLoopBackOff` |
| 2. Warm-up | 15 min | 2% | Passive observation; no alerts should fire | Zero `CanaryP99LatencyRegression` or `CanaryErrorRateElevated` firings |
| 3. First-hour watch | 60 min | 2% | On-call actively monitors dashboard; **rollback window is open** | All §3 pass criteria hold for the full 60 min |
| 4. Soak | 6 hours | 2% | Passive with paging on §3 fail criteria | Continue for full 6h without page |
| 5. Ramp (day 2) | 24 h | 10% (5/50) | Scale canary → 5, baseline → 45 | Same criteria at 10% traffic share |
| 6. Full rollout (day 5) | — | 100% | Retire baseline: promote canary image via normal rolling update | — |

**This playbook covers phases 0–3.** Phases 4–6 reuse the same alerts and pass/fail table — just with wider replica counts.

---

## 2. Traffic-splitting mechanism

### 2.1 How 2% is enforced

The collector fleet is fronted by a **headless Kubernetes Service** (`clusterIP: None`) that publishes SRV records for every pod. Upstream OTLP/gRPC clients use gRPC's built-in `dns:///` resolver with the default `round_robin` load-balancing policy, which distributes RPCs uniformly across resolved endpoints.

With 49 baseline pods + 1 canary pod, all sharing the same Service selector:

$$
\text{Canary traffic share} = \frac{1}{49 + 1} = 2.00\%
$$

This is enforced entirely by pod count — no service mesh, no Envoy weights, no header-based routing. The trade-off: traffic is uniform across pods, which is exactly what we want for a load-representative canary. See [`k8s/collector-daemonset.yaml`](./otel_canary/k8s/collector-daemonset.yaml).

### 2.2 Why not use a service mesh?

An Istio `VirtualService` with `weight: 2` on the canary subset works but adds two failure modes: (a) mesh sidecar CPU on the collector adds ~5% overhead that biases the comparison; (b) mesh-level retries can mask exporter-level errors we're specifically trying to detect. Pod-count-based splitting is simpler and comparison-clean.

### 2.3 Label discriminator

Both Deployments carry `canary.otel.io/track` ∈ `{canary, control}`. This label:

* is scraped by Prometheus (attribute promotion via `podMonitor.podTargetLabels`)
* becomes the `canary_otel_io_track` series label (Prometheus label sanitizer replaces `.` and `/` with `_`)
* is the sole `sum by (...)` dimension in every recording rule in [`prometheus/canary-recording-rules.yaml`](./otel_canary/prometheus/canary-recording-rules.yaml)

Every Grafana panel is `by (canary_otel_io_track)` so canary and control render as two lines on the same chart with no other clutter.

---

## 3. Monitoring setup

### 3.1 Internal-telemetry configuration

Both Deployments must expose OTel internal metrics on port 8888 via the Prometheus exporter, at `telemetry.metrics.level: normal`. The `normal` level (default since [collector #7890](https://github.com/open-telemetry/opentelemetry-collector/issues/7890)) gives us the four metric families we need:

* **Exporter counters** — `otelcol_exporter_sent_spans`, `otelcol_exporter_send_failed_spans`, `otelcol_exporter_enqueue_failed_spans` ([OpenTelemetry Internal Telemetry docs](https://opentelemetry.io/docs/collector/internal-telemetry/))
* **Exporter histogram** — `otelcol_exporter_send_duration` (send-side wall-clock)
* **LoadBalancing histogram** — `otelcol_loadbalancer_backend_latency` ([OTel Scaling docs](https://opentelemetry.io/docs/collector/scaling/)) — this is the router-lookup metric we're actually trying to move
* **Queue gauges** — `otelcol_exporter_queue_size`, `otelcol_exporter_queue_capacity`
* **Process metrics** — `otelcol_process_cpu_seconds_total`, `otelcol_process_memory_rss`, `otelcol_process_runtime_total_alloc_bytes_total` ([Datadog Collector Health docs](https://docs.datadoghq.com/opentelemetry/integrations/collector_health_metrics/))

### 3.2 Recording rules

The [PrometheusRule](./otel_canary/prometheus/canary-recording-rules.yaml) pre-computes the seven signals shipped to Grafana:

| Rule name | What it means |
|---|---|
| `canary:loadbalancer_backend_latency_seconds:p99` | Router-lookup p99, per track |
| `canary:exporter_send_duration_seconds:p99` | End-to-end send p99, per track |
| `canary:exporter_error_rate:ratio5m` | Failed / total, per track |
| `canary:exporter_enqueue_failure_rate:ratio5m` | Backpressure indicator |
| `canary:exporter_queue_utilization:ratio` | Queue size / capacity |
| `canary:process_cpu_utilization:ratio5m` | CPU seconds/second per pod |
| `canary:process_memory_utilization:ratio` | RSS / limit |
| `canary:process_heap_alloc_rate:bytes5m` | GC-pressure proxy |
| `canary:p99_speedup:ratio` | **`control_p99 / canary_p99`** — the load-test-lower-bound test |
| `canary:error_rate:delta` | `canary_err / control_err` |

Pre-computing means the Grafana dashboard renders in one query per panel, and alert expressions stay readable.

### 3.3 Dashboard

[`grafana/canary-dashboard.json`](./otel_canary/grafana/canary-dashboard.json) is an 8-panel dashboard sized for a single laptop screen. Layout:

```
┌─ p99 latency (canary vs control) ────┬─ speedup   ─┐
│                                      │  ratio      │
│                                      ├─ error rate ┤
│                                      │  delta      │
├─ CPU ──────┬─ memory ────┬─ queue ───┴─────────────┤
│            │             │                          │
├─ heap alloc rate ──────────┬─ enqueue-failure rate ─┤
│                            │                        │
└────────────────────────────┴────────────────────────┘
```

Key: canary is the **red** series, control is **blue**, on every panel. Every panel has an explicit threshold that matches the alert in §4.2.

---

## 4. Pass / fail criteria

### 4.1 Pass criteria (all must hold across the 60-min first-hour watch)

| # | Metric | Threshold | Anchoring |
|---|---|---|---|
| **P1** | `canary:p99_speedup:ratio` | **≥ 1.4× sustained** (≥ 1.7× is target) | [Load-test lower bound](./lbloadtest_plan.md) |
| **P2** | `canary:loadbalancer_backend_latency_seconds:p99` (canary) | ≤ 1.0× control | Never worse than baseline |
| **P3** | `canary:exporter_error_rate:ratio5m` (canary) | ≤ max(control × 1.5, 0.005) | Absolute floor at 0.5% |
| **P4** | `canary:exporter_enqueue_failure_rate:ratio5m` (canary) | = 0 | Any drop = fail |
| **P5** | `canary:process_cpu_utilization:ratio5m` (canary) | ≤ 1.10× control | Atomic should be **cheaper**, tolerating 10% noise |
| **P6** | `canary:process_memory_utilization:ratio` (canary) | ≤ 1.20× control **and** < 0.75 absolute | 20% drift OK for extra snapshot object; 75% is GC-storm line |
| **P7** | `canary:exporter_queue_utilization:ratio` (canary) | < 0.60 | OTel scaling guidance: 0.70 → scale, we take a 10-point margin |
| **P8** | `canary:process_heap_alloc_rate:bytes5m` (canary) | ≤ 1.30× control | Atomic swap allocates a new snapshot on writes; 30% for 500ms churn is expected |
| **P9** | Alert count | Zero `severity=page` firings | Any auto-rollback signal = fail |

### 4.2 Fail criteria (any one triggers rollback)

| # | Alert | PromQL (verbatim from the PrometheusRule) | Severity | Action |
|---|---|---|---|---|
| **F1** | `CanaryP99LatencyRegression` | `p99_canary > 1.2 × p99_control for 5m` | `page` | Auto-rollback |
| **F2** | `CanaryErrorRateElevated` | `error_rate_canary > 0.01 AND delta > 3 for 5m` | `page` | Auto-rollback |
| **F3** | `CanaryQueueUtilizationHigh` | `queue_util_canary > 0.7 for 10m` | `slack` | Manual review |
| **F4** | `CanaryCPUElevated` | `cpu_canary > 1.25 × cpu_control for 15m` | `slack` | Manual review |
| **F5** | `CanaryMemoryElevated` | `mem_canary > 1.30 × mem_control for 15m` | `slack` | Manual review |
| **F6** | `CanaryGCPressureElevated` | `heap_alloc_canary > 1.50 × heap_alloc_control for 15m` | `slack` | Manual review |
| **F7** | `CanarySpeedupBelowLowerBound` | `speedup < 1.4 for 30m` | `slack` | Investigate — not fail |

`severity=page` triggers PagerDuty *and* posts to `#otel-canary-alerts`. `severity=slack` posts only. Any Slack post from these alerts should be followed within the alert message by a suggested `/otel-canary-rollback reason=<alert>` command.

### 4.3 Why these thresholds?

* **P1 = 1.4× tolerance below 1.7×.** The 1.7× is a *lab* lower bound at synthetic contention. Real workloads have GC, network jitter, and non-router CPU that dilute the effect. Historical experience with similar refactors ([golang/go#17973](https://github.com/golang/go/issues/17973)) shows a ~20% haircut lab→prod is normal.
* **F1 threshold at +20% latency.** Below the 5% coefficient-of-variation observed in the load-test runs (see report generator output in `lbloadtest_plan.md` §5), so we're outside noise but not overly sensitive.
* **F2 requires both absolute (>1%) AND relative (>3×).** Absolute-only alarms during backend hiccups; relative-only alarms on any baseline blip. Both together = signal.
* **P5 tolerance 10% CPU.** The atomic path removes a lock acquire; **CPU should drop**. Any positive delta up to noise (10%) is tolerable; systematic increase means an unrelated regression slipped in.
* **P6 memory 20% + absolute 75%.** `atomic.Pointer[T]` holds one extra live snapshot during the swap (old + new co-exist briefly). At 32 backends × 100 vnodes × ~100 bytes each = ~320 KB per snapshot — negligible. Any observed >20% drift means allocations elsewhere.

---

## 5. Slack rollback flow

### 5.1 Command surface

```
/otel-canary-rollback reason=<short-string>
```

Registered as a Slack Slash Command pointing at `https://otel-canary.example.com/slack/rollback` — see [`slack/k8s-deployment.yaml`](./otel_canary/slack/k8s-deployment.yaml) for the Ingress config.

### 5.2 What happens when it's invoked

```
User:  /otel-canary-rollback reason=p99-regression
       │
       ├──► Slack POSTs to /slack/rollback with HMAC-SHA256 signature
       │
Bot:   │   1. Verify signature (rejects if timestamp > 5 min or HMAC mismatch)
       │   2. Check user_id ∈ ALLOWED_USERS (else :no_entry: response)
       │   3. Idempotency: reject if another rollback started in the last 5 min
       │   4. Immediate ACK to Slack (:rotating_light: banner) — must be < 3s
       │   5. Spawn background thread → do_rollback()
       │
       │   Meanwhile in the background:
       │      ├─ kubectl scale deploy/otel-collector-canary --replicas=0
       │      ├─ kubectl scale deploy/otel-collector-baseline --replicas=50
       │      └─ kubectl rollout status deploy/otel-collector-baseline --timeout=180s
       │
Bot:   │   6. Post progress to response_url after each step
       │   7. Final :white_check_mark: with next-step guidance
       │   8. Append JSON audit entry to /var/log/canary-rollback.jsonl
       ▼
```

Full implementation in [`slack/rollback_bot.py`](./otel_canary/slack/rollback_bot.py) — 280 lines of Flask + `hmac.compare_digest` + `subprocess.run(["kubectl", ...])`.

### 5.3 Security model

* **Authentication:** Slack HMAC-SHA256 signature per [Slack's spec](https://api.slack.com/authentication/verifying-requests-from-slack). Anti-replay: 5-min timestamp window.
* **Authorization:** Slack user ID must be in the `ALLOWED_USERS` env var (secret). Not display name, not email — the immutable `U0123ABCD` ID.
* **Least-privilege RBAC:** The bot's ServiceAccount can only `patch` the `scale` subresource on two named Deployments — no create, no delete, no exec, no secrets access. See the Role definition in [`slack/k8s-deployment.yaml`](./otel_canary/slack/k8s-deployment.yaml).
* **Audit log:** Every invocation (authorized, unauthorized, and completed) is appended to `/var/log/canary-rollback.jsonl` for post-incident review.
* **Idempotency:** Duplicate submits within 5 min return `:hourglass:` without triggering a second scale operation — prevents a nervous operator from double-scaling baseline.

### 5.4 Break-glass path

If the bot itself is unhealthy (e.g., during a broader outage), on-call runs [`scripts/manual-rollback.sh`](./otel_canary/scripts/manual-rollback.sh) directly against the cluster. Same three-step sequence, same audit-log schema, but requires human `ROLLBACK` confirmation at stdin.

---

## 6. First-hour operator runbook

**T–15 min:** Deploy prerequisites.

```bash
kubectl apply -f prometheus/canary-recording-rules.yaml
kubectl apply -f slack/k8s-deployment.yaml       # bot + RBAC + Ingress
```

Verify: `kubectl -n observability get pods -l app.kubernetes.io/name=otel-canary-rollback-bot` — 2 pods `Ready`. Hit `https://otel-canary.example.com/healthz` — expect `{"ok": true}`.

**T–0:** Ignition.

```bash
kubectl apply -f k8s/collector-daemonset.yaml
kubectl -n observability rollout status deploy/otel-collector-canary --timeout=120s
```

Verify: the canary pod appears in `kubectl get endpoints otel-collector -o yaml`.

**T+0 to T+5 min:** Warm-up watch. Open the Grafana dashboard. Both series should render within 60s. **Expected shape:**

* `p99 latency (canary)` line settles below `p99 latency (control)` within 90 s.
* `speedup ratio` stat panel enters the green zone (≥ 1.7) within 5 min. If not, drop to `orange` zone (≥ 1.4) is acceptable during warm-up; stays there = fail (F7).
* All error/queue/enqueue lines flat at zero.

**T+5 to T+60 min:** Active watch. On-call keeps the dashboard open. **Any red or orange panel demands attention.**

* If `CanaryP99LatencyRegression` fires → the auto-rollback labels are set, but auto-rollback is manual for the first hour. Confirm from the dashboard, then in Slack: `/otel-canary-rollback reason=F1-auto`.
* If any `severity=slack` alert fires → inspect the corresponding panel; if it's a real trend (not a scrape-interval blip), invoke `/otel-canary-rollback reason=<alert-name>`.
* If nothing fires but `speedup ratio` is < 1.4 for 30 min → not a rollback trigger, but page the SOP author to review whether prod workload is actually router-lock-dominated. This means the refactor is technically correct but not solving the assumed problem.

**T+60 min:** First-hour gate.

* All 9 pass criteria (P1–P9) hold → advance to phase 4 (6-hour soak, passive).
* Any fail → rollback via Slack, file bug, attach dashboard export.

---

## 7. Rollback drills

Before the actual rollout, run these two drills against a staging cluster:

1. **Happy-path drill.** Deploy the canary against a staging fleet. Invoke `/otel-canary-rollback reason=drill-happy`. Verify: canary at 0 replicas within 30s; audit log has one `rollback_started` and one `rollback_complete` entry; Slack channel shows the three progress messages.

2. **Unauthorized-user drill.** Have someone not in `ALLOWED_USERS` invoke the command. Verify: ephemeral `:no_entry:` response; audit log has `rollback_unauthorized`; no kubectl calls made.

Only after both drills pass in staging does the prod rollout proceed.

---

## 8. Deliverables in this package

| Path | Purpose |
|---|---|
| `otel_canary_playbook.md` | This document. |
| `otel_canary/k8s/collector-daemonset.yaml` | Baseline (49) + canary (1) Deployments + headless Service + PDB |
| `otel_canary/prometheus/canary-recording-rules.yaml` | 9 recording rules + 7 alerts |
| `otel_canary/grafana/canary-dashboard.json` | 8-panel side-by-side comparison dashboard |
| `otel_canary/slack/rollback_bot.py` | Flask signature-verified slash-command handler (280 LOC) |
| `otel_canary/slack/Dockerfile` | Distroless-adjacent Python bot image |
| `otel_canary/slack/requirements.txt` | pinned deps: Flask 3.0.3, requests 2.32.3, gunicorn 23.0 |
| `otel_canary/slack/k8s-deployment.yaml` | Bot Deployment + Service + Ingress + least-privilege RBAC |
| `otel_canary/scripts/manual-rollback.sh` | Break-glass kubectl script when the bot itself is down |

---

## 9. Cross-references to earlier series artifacts

* **Why atomic.Pointer at all?** See [SOP §2.2](./otel_exporter_concurrency_sop.md).
* **Why LoadBalancing exporter specifically?** See [contrib comparison](./otel_exporter_concurrency_comparison.md) — LoadBalancing scored 7/15 vs OTLP/Kafka at 15/15.
* **Where does 1.7× come from?** See [`lbbench_analysis.md`](./lbbench_analysis.md) — 4-128 goroutines on 2 vCPU.
* **Does 1.7× hold at scale?** See [`lbloadtest_plan.md`](./lbloadtest_plan.md) — 64-core GitHub Actions matrix expected to show 3.5×–5.5×.
* **Does 1.7× hold in prod?** This document. First real answer arrives 65 min after `kubectl apply`.
