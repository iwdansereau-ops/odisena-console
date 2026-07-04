# 🧠 Memory Regression Dashboard — `org/example-corp`

_Generated: 2026-07-02T05:59:41Z_

| Metric | Count |
|---|---:|
| Repositories scanned | 14 |
| Onboarded (workflow configured) | 12 |
| 🔺 Regressing | **4** |
| ✅ Clean | 7 |
| ❓ Evaluator errored | 1 |
| ⚪ No verdict yet | 2 |

## 🚨 4 service(s) with active memory regressions

| Verdict | Repository | Ref | Worst offender | Δ retained | Evidence |
|---|---|---|---|---:|---|
| ☣️ **Leak + churn** | [example-corp/payment-gateway](https://github.com/example-corp/payment-gateway) | `2a0f8d7` on `main` | `(*txnCoalescer).flush` | 1.5 MiB | [open](https://github.com/example-corp/payment-gateway/actions/runs/91221) |
| 🔺 **Retention leak** | [example-corp/order-router](https://github.com/example-corp/order-router) | [PR #913](https://github.com/example-corp/order-router/pull/913) `c17aa72` | `(*orderCache).Put` | 793.4 KiB | [open](https://github.com/example-corp/order-router/actions/runs/42088) |
| 🔺 **Retention leak** | [example-corp/warehouse-sync](https://github.com/example-corp/warehouse-sync) | `9988aa7` on `main` | `(*shelfIndex).snapshot` | 340.7 KiB | [open](https://github.com/example-corp/warehouse-sync/actions/runs/56010) |
| 🌀 **Allocation churn** | [example-corp/notifier](https://github.com/example-corp/notifier) | [PR #214](https://github.com/example-corp/notifier/pull/214) `aa11bb2` | `—` | ? | [open](https://github.com/example-corp/notifier/actions/runs/17845) |

### Per-repo detail

#### ☣️ `example-corp/payment-gateway` — Leak + churn
- **main** `2a0f8d7` — Leak + churn: Retention leak + allocation churn detected.  
  [workflow run](https://github.com/example-corp/payment-gateway/actions/runs/91221)

#### 🔺 `example-corp/order-router` — Retention leak
- **PR #913** [cache orders by tenant id](https://github.com/example-corp/order-router/pull/913) `c17aa72` — Retention leak: Retention leak: (*orderCache).Put +812432 B (flat).

#### 🔺 `example-corp/warehouse-sync` — Retention leak
- **main** `9988aa7` — Retention leak: Retention leak: (*shelfIndex).snapshot +348901 B (flat).  
  [workflow run](https://github.com/example-corp/warehouse-sync/actions/runs/56010)

#### 🌀 `example-corp/notifier` — Allocation churn
- **PR #214** [switch to fan-out worker pool](https://github.com/example-corp/notifier/pull/214) `aa11bb2` — Allocation churn: Allocation churn / GC thrash detected.
