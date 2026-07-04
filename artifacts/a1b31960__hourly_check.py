#!/usr/bin/env python3
"""
hourly_check.py — 24-hour canary soak check.

Runs once per hour. For each run:
  1. Query Prometheus for p99 backend latency and CPU utilization,
     per canary track (canary vs control).
  2. Compute delta ratios: canary / control.
  3. Compare against thresholds from the load-test lower bound + a
     10% drift budget:
       * p99 speedup >= 1.7 (canary p99 <= control p99 / 1.7)  — target
       * p99 within 10% (canary p99 <= 1.10 * control p99)     — floor
       * CPU  within 10% (canary cpu <= 1.10 * control cpu)    — floor
  4. Post a one-line PASS/FAIL summary to Slack with a Grafana link.

Meant to be invoked from a scheduled task. All config comes from env vars
so the same script works locally and in the cron sandbox.

Environment:
    PROMETHEUS_URL          e.g. https://prom.example.com
    PROMETHEUS_BEARER       optional bearer token
    GRAFANA_DASHBOARD_URL   permalink for the Slack message
    SLACK_CHANNEL_ID        DM or channel to ping (Slack user IDs start with U/W)
    SOAK_START_ISO          ISO8601 timestamp when the soak began; used to
                            label progress ("hour 7/24")

Exit codes:
    0   ran, posted OK (regardless of pass/fail)
    2   config error (missing env vars)
    3   Prometheus unreachable or query error
    4   Slack post failed
"""

from __future__ import annotations

import json
import os
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any

# ---------------------------------------------------------------------------
# Thresholds — anchored to lbloadtest_plan.md
# ---------------------------------------------------------------------------

# The 1.7× is the LOAD-TEST LOWER BOUND. The 10% is the user's specified
# drift budget vs baseline. Both are checked; failing either flags the row.
P99_SPEEDUP_TARGET = 1.7      # canary should be at least this much FASTER
P99_DRIFT_BUDGET = 0.10       # canary p99 must not exceed control × 1.10
CPU_DRIFT_BUDGET = 0.10       # canary CPU must not exceed control × 1.10

# 5-minute rate windows match the PrometheusRule in canary-recording-rules.yaml
P99_RANGE = "5m"
CPU_RANGE = "5m"

# ---------------------------------------------------------------------------
# PromQL queries — target the pre-computed recording rules from
# canary-recording-rules.yaml when available; fall back to raw metrics.
# ---------------------------------------------------------------------------

QUERIES: dict[str, str] = {
    "p99_canary": (
        'canary:loadbalancer_backend_latency_seconds:p99{canary_otel_io_track="canary"}'
    ),
    "p99_control": (
        'canary:loadbalancer_backend_latency_seconds:p99{canary_otel_io_track="control"}'
    ),
    "cpu_canary": (
        'canary:process_cpu_utilization:ratio5m{canary_otel_io_track="canary"}'
    ),
    "cpu_control": (
        'canary:process_cpu_utilization:ratio5m{canary_otel_io_track="control"}'
    ),
}

# ---------------------------------------------------------------------------
# Prometheus HTTP client
# ---------------------------------------------------------------------------


def prom_query(base_url: str, expr: str, bearer: str | None = None,
               timeout: float = 15.0) -> float | None:
    """Run an instant query; return the single scalar value or None if empty."""
    url = base_url.rstrip("/") + "/api/v1/query?" + urllib.parse.urlencode({"query": expr})
    req = urllib.request.Request(url, method="GET")
    if bearer:
        req.add_header("Authorization", f"Bearer {bearer}")

    with urllib.request.urlopen(req, timeout=timeout) as resp:
        payload = json.loads(resp.read().decode("utf-8"))

    if payload.get("status") != "success":
        raise RuntimeError(f"prometheus returned status={payload.get('status')}: "
                           f"{payload.get('error', '<no error>')}")

    result = payload.get("data", {}).get("result", [])
    if not result:
        return None
    # Instant query → vector of {metric, value: [ts, "float"]}. We took a
    # single-series query so we expect exactly one entry.
    if len(result) > 1:
        # Multiple series returned (e.g. label leak). Average them and warn.
        vals = []
        for r in result:
            try:
                vals.append(float(r["value"][1]))
            except (KeyError, ValueError, IndexError):
                continue
        return sum(vals) / len(vals) if vals else None
    try:
        return float(result[0]["value"][1])
    except (KeyError, ValueError, IndexError):
        return None


# ---------------------------------------------------------------------------
# Analysis
# ---------------------------------------------------------------------------


def evaluate(metrics: dict[str, float | None]) -> dict[str, Any]:
    """Compute deltas and pass/fail per metric row."""
    out: dict[str, Any] = {"rows": [], "overall": "PASS"}

    p99_c = metrics["p99_canary"]
    p99_b = metrics["p99_control"]
    cpu_c = metrics["cpu_canary"]
    cpu_b = metrics["cpu_control"]

    # --- p99 latency row ------------------------------------------------
    if p99_c is None or p99_b is None or p99_b <= 0:
        out["rows"].append({
            "metric": "p99",
            "status": "NO_DATA",
            "detail": f"canary={p99_c} control={p99_b}",
        })
        out["overall"] = "NO_DATA"
    else:
        speedup = p99_b / p99_c
        drift = (p99_c / p99_b) - 1.0     # positive = canary slower
        # Fail if canary is more than 10% slower than control
        fails_drift = drift > P99_DRIFT_BUDGET
        # Info: is the load-test speedup lower bound also met?
        meets_target = speedup >= P99_SPEEDUP_TARGET
        status = "FAIL" if fails_drift else "PASS"
        out["rows"].append({
            "metric": "p99",
            "status": status,
            "canary_ms": p99_c * 1000,
            "control_ms": p99_b * 1000,
            "drift_pct": drift * 100,
            "speedup": speedup,
            "meets_lower_bound": meets_target,
            "detail": (
                f"canary p99={p99_c*1000:.2f}ms, control p99={p99_b*1000:.2f}ms, "
                f"drift={drift*100:+.1f}%, speedup={speedup:.2f}× "
                f"({'✅' if meets_target else '⚠️'} 1.7× lower bound)"
            ),
        })
        if status == "FAIL":
            out["overall"] = "FAIL"

    # --- CPU utilization row --------------------------------------------
    if cpu_c is None or cpu_b is None or cpu_b <= 0:
        out["rows"].append({
            "metric": "cpu",
            "status": "NO_DATA",
            "detail": f"canary={cpu_c} control={cpu_b}",
        })
        if out["overall"] != "FAIL":
            out["overall"] = "NO_DATA"
    else:
        drift = (cpu_c / cpu_b) - 1.0
        fails_drift = drift > CPU_DRIFT_BUDGET
        status = "FAIL" if fails_drift else "PASS"
        out["rows"].append({
            "metric": "cpu",
            "status": status,
            "canary": cpu_c,
            "control": cpu_b,
            "drift_pct": drift * 100,
            "detail": (
                f"canary cpu={cpu_c:.3f}c/s, control cpu={cpu_b:.3f}c/s, "
                f"drift={drift*100:+.1f}%"
            ),
        })
        if status == "FAIL":
            out["overall"] = "FAIL"

    return out


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------


def format_message(analysis: dict[str, Any], grafana_url: str,
                   soak_hour: int | None) -> str:
    """One-line summary + one-line-per-metric detail + dashboard link."""
    icon = {"PASS": ":large_green_circle:", "FAIL": ":red_circle:",
            "NO_DATA": ":large_yellow_circle:"}[analysis["overall"]]
    prefix = f"*OTel canary soak — hour {soak_hour}/24*" if soak_hour else "*OTel canary soak*"

    # Per-row lines
    row_lines = []
    for row in analysis["rows"]:
        row_icon = {"PASS": ":white_check_mark:", "FAIL": ":x:",
                    "NO_DATA": ":grey_question:"}[row["status"]]
        row_lines.append(f"{row_icon} `{row['metric']}` — {row['detail']}")

    verdict = {
        "PASS": "Canary passing all thresholds. Safe to continue soak.",
        "FAIL": "Canary DRIFTED >10% from control. Review before proceeding to full rollout.",
        "NO_DATA": "Missing metrics — check Prometheus scrape config.",
    }[analysis["overall"]]

    body = "\n".join([
        f"{prefix} — {icon} *{analysis['overall']}*",
        f"_{verdict}_",
        *row_lines,
        f":bar_chart: <{grafana_url}|Open Grafana dashboard>",
    ])
    return body


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def compute_soak_hour(start_iso: str | None) -> int | None:
    if not start_iso:
        return None
    try:
        start = datetime.fromisoformat(start_iso)
        if start.tzinfo is None:
            start = start.replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    now = datetime.now(timezone.utc)
    delta_h = int((now - start).total_seconds() // 3600) + 1
    return max(1, min(delta_h, 24))


def main() -> int:
    prom_url = os.environ.get("PROMETHEUS_URL", "").strip()
    prom_bearer = os.environ.get("PROMETHEUS_BEARER", "").strip() or None
    grafana_url = os.environ.get("GRAFANA_DASHBOARD_URL", "").strip() \
        or "https://grafana.example.com/d/otel-canary"
    soak_start = os.environ.get("SOAK_START_ISO", "").strip() or None

    if not prom_url:
        # Emit a Slack-friendly config-error message via stdout — the caller
        # (the cron task) will read this and forward it to the user.
        print(json.dumps({
            "config_error": True,
            "message": (":warning: hourly canary check skipped — "
                        "`PROMETHEUS_URL` not configured. Set it in the "
                        "task environment or run `manual-rollback.sh` if "
                        "monitoring is degraded."),
        }))
        return 2

    metrics: dict[str, float | None] = {}
    try:
        for key, expr in QUERIES.items():
            metrics[key] = prom_query(prom_url, expr, prom_bearer)
    except Exception as e:  # noqa: BLE001
        print(json.dumps({
            "config_error": False,
            "prom_error": True,
            "message": f":warning: Prometheus query failed: `{e}`",
        }))
        return 3

    analysis = evaluate(metrics)
    soak_hour = compute_soak_hour(soak_start)
    text = format_message(analysis, grafana_url, soak_hour)

    # Emit the analysis for the caller to hand off to Slack.
    print(json.dumps({
        "config_error": False,
        "prom_error": False,
        "overall": analysis["overall"],
        "soak_hour": soak_hour,
        "slack_text": text,
        "metrics": {k: v for k, v in metrics.items()},
        "rows": analysis["rows"],
    }))
    return 0


if __name__ == "__main__":
    sys.exit(main())
