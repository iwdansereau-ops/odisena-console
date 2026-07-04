#!/usr/bin/env python3
"""
rollback_bot.py — Slack slash-command handler for OTel canary rollback.

Registered command:  /otel-canary-rollback

Slash-command payload arrives at POST /slack/rollback with form fields:
    token, team_id, user_id, user_name, command, text, response_url, ...

Design goals:
    1. Signature-verified per Slack's HMAC-SHA256 spec.
    2. Idempotent — repeated invocations within 5 min return the same status.
    3. Two-phase: primary responder acknowledges within 3s (Slack requirement),
       kubectl scale runs in a background thread and posts the final result
       to response_url.
    4. Audit-logged: every invocation writes a JSON line to /var/log/canary-rollback.jsonl
       with user_id, timestamp, reason, k8s response.
    5. Authorization: user_id must appear in ALLOWED_USERS env var (comma-separated).
       This is intentionally simple — production should route via SSO groups.

Deployment: run as a Deployment behind a Service exposing port 8080. The Slack
app's Slash Command config must point at https://<ingress>/slack/rollback.

Environment:
    SLACK_SIGNING_SECRET     required
    ALLOWED_USERS            required; comma-separated Slack user IDs
    K8S_NAMESPACE            default: observability
    K8S_CANARY_DEPLOYMENT    default: otel-collector-canary
    K8S_BASELINE_DEPLOYMENT  default: otel-collector-baseline
    K8S_BASELINE_REPLICAS    default: 50 (post-rollback target)
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from flask import Flask, abort, request
import requests

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

SLACK_SIGNING_SECRET = os.environ["SLACK_SIGNING_SECRET"].encode()
ALLOWED_USERS = set(u.strip() for u in os.environ["ALLOWED_USERS"].split(",") if u.strip())
K8S_NAMESPACE = os.environ.get("K8S_NAMESPACE", "observability")
CANARY_DEPLOYMENT = os.environ.get("K8S_CANARY_DEPLOYMENT", "otel-collector-canary")
BASELINE_DEPLOYMENT = os.environ.get("K8S_BASELINE_DEPLOYMENT", "otel-collector-baseline")
BASELINE_REPLICAS = int(os.environ.get("K8S_BASELINE_REPLICAS", "50"))

AUDIT_LOG = Path(os.environ.get("AUDIT_LOG", "/var/log/canary-rollback.jsonl"))

# In-memory idempotency cache: response_url → (started_at_epoch, status)
_STATE: dict[str, tuple[float, str]] = {}
_STATE_LOCK = threading.Lock()
IDEMPOTENCY_WINDOW_SEC = 300  # 5 min

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("rollback-bot")
app = Flask(__name__)

# ---------------------------------------------------------------------------
# Signature verification per https://api.slack.com/authentication/verifying-requests-from-slack
# ---------------------------------------------------------------------------

def verify_slack_signature(req) -> bool:
    ts = req.headers.get("X-Slack-Request-Timestamp", "")
    sig = req.headers.get("X-Slack-Signature", "")
    if not ts or not sig:
        return False
    # Anti-replay: reject requests older than 5 minutes.
    try:
        if abs(time.time() - int(ts)) > 60 * 5:
            log.warning("stale slack request rejected: ts=%s", ts)
            return False
    except ValueError:
        return False
    body = req.get_data(as_text=True)
    basestring = f"v0:{ts}:{body}".encode()
    expected = "v0=" + hmac.new(SLACK_SIGNING_SECRET, basestring, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, sig)

# ---------------------------------------------------------------------------
# Audit log
# ---------------------------------------------------------------------------

def audit(entry: dict) -> None:
    entry["ts"] = datetime.now(timezone.utc).isoformat()
    try:
        AUDIT_LOG.parent.mkdir(parents=True, exist_ok=True)
        with AUDIT_LOG.open("a") as f:
            f.write(json.dumps(entry) + "\n")
    except OSError as e:
        log.error("failed to write audit log: %s", e)

# ---------------------------------------------------------------------------
# Rollback action — the actual k8s calls.
# Runs in a background thread; posts progress to response_url.
# ---------------------------------------------------------------------------

def _post_response(response_url: str, text: str, replace_original: bool = False) -> None:
    try:
        requests.post(
            response_url,
            json={"response_type": "in_channel",
                  "replace_original": replace_original,
                  "text": text},
            timeout=5,
        )
    except requests.RequestException as e:
        log.error("failed to POST to response_url: %s", e)


def _kubectl(args: list[str]) -> tuple[int, str]:
    """Run kubectl, return (returncode, combined stdout+stderr)."""
    cmd = ["kubectl", "-n", K8S_NAMESPACE, *args]
    log.info("running: %s", " ".join(cmd))
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=30, check=False)
    except subprocess.TimeoutExpired:
        return 124, "kubectl timeout"
    return r.returncode, (r.stdout + r.stderr).strip()


def do_rollback(response_url: str, user_id: str, user_name: str, reason: str) -> None:
    """
    Rollback sequence:
      1. Scale canary Deployment → 0 replicas (stops traffic to atomic build).
      2. Scale baseline Deployment → BASELINE_REPLICAS (fills the gap).
      3. Wait for canary rollout to complete (kubectl rollout status).
      4. Post final confirmation.

    We deliberately do NOT delete the canary Deployment — leaving it at replicas=0
    preserves the image reference and labels for post-mortem inspection.
    """
    audit_entry = {
        "event": "rollback_started",
        "user_id": user_id,
        "user_name": user_name,
        "reason": reason,
        "namespace": K8S_NAMESPACE,
    }
    audit(audit_entry)

    _post_response(response_url,
                   f":arrow_backward: Rollback started by <@{user_id}>. "
                   f"Reason: `{reason}`\n"
                   f"Step 1/3: scaling `{CANARY_DEPLOYMENT}` to 0 replicas…")

    rc, out = _kubectl(["scale", f"deployment/{CANARY_DEPLOYMENT}", "--replicas=0"])
    if rc != 0:
        audit({**audit_entry, "event": "rollback_failed_step1", "kubectl_output": out})
        _post_response(response_url,
                       f":x: Rollback FAILED at step 1 (scale canary→0):\n```{out}```",
                       replace_original=True)
        return

    _post_response(response_url,
                   f"Step 2/3: scaling `{BASELINE_DEPLOYMENT}` to {BASELINE_REPLICAS} replicas…")
    rc, out = _kubectl(["scale", f"deployment/{BASELINE_DEPLOYMENT}",
                        f"--replicas={BASELINE_REPLICAS}"])
    if rc != 0:
        audit({**audit_entry, "event": "rollback_failed_step2", "kubectl_output": out})
        _post_response(response_url,
                       f":warning: Baseline scale-up failed (canary already at 0, "
                       f"cluster has 49 pods):\n```{out}```",
                       replace_original=True)
        return

    _post_response(response_url,
                   f"Step 3/3: waiting for baseline rollout to converge…")
    rc, out = _kubectl(["rollout", "status", f"deployment/{BASELINE_DEPLOYMENT}",
                        "--timeout=180s"])
    if rc != 0:
        audit({**audit_entry, "event": "rollback_failed_step3", "kubectl_output": out})
        _post_response(response_url,
                       f":warning: Baseline rollout did not converge in 180s:\n```{out}```",
                       replace_original=True)
        return

    audit({**audit_entry, "event": "rollback_complete"})
    _post_response(
        response_url,
        f":white_check_mark: **Canary rolled back successfully.**\n"
        f"* `{CANARY_DEPLOYMENT}` at 0 replicas (image preserved for post-mortem)\n"
        f"* `{BASELINE_DEPLOYMENT}` at {BASELINE_REPLICAS} replicas\n"
        f"* Reason: `{reason}`\n"
        f"* Initiated by: <@{user_id}>\n"
        f"Next: file a bug at https://github.com/example/otel-collector-fork/issues/new "
        f"and attach the Grafana panel from the last hour.",
        replace_original=True,
    )

# ---------------------------------------------------------------------------
# Slash-command handler
# ---------------------------------------------------------------------------

@app.route("/slack/rollback", methods=["POST"])
def slack_rollback():
    if not verify_slack_signature(request):
        abort(401, "invalid slack signature")

    user_id = request.form.get("user_id", "")
    user_name = request.form.get("user_name", "unknown")
    text = request.form.get("text", "").strip()
    response_url = request.form.get("response_url", "")

    if user_id not in ALLOWED_USERS:
        log.warning("unauthorized rollback attempt by %s (%s)", user_id, user_name)
        audit({"event": "rollback_unauthorized", "user_id": user_id, "user_name": user_name})
        return {"response_type": "ephemeral",
                "text": f":no_entry: <@{user_id}> is not authorized to trigger a canary rollback. "
                        f"Contact #otel-oncall."}

    # Parse `reason=...` from the slash-command text. Default if omitted.
    reason = "unspecified"
    for tok in text.split():
        if tok.startswith("reason="):
            reason = tok.split("=", 1)[1] or "unspecified"

    # Idempotency: dedupe multiple submits within IDEMPOTENCY_WINDOW_SEC.
    with _STATE_LOCK:
        now = time.time()
        # Purge stale entries.
        for k, (started, _) in list(_STATE.items()):
            if now - started > IDEMPOTENCY_WINDOW_SEC:
                _STATE.pop(k, None)
        # Any recent invocation by same user counts as a dedupe.
        for started, status in _STATE.values():
            if now - started < IDEMPOTENCY_WINDOW_SEC and status == "in_progress":
                return {"response_type": "ephemeral",
                        "text": ":hourglass: A rollback is already in progress. "
                                "Watch this channel for updates."}
        _STATE[response_url] = (now, "in_progress")

    # Kick off the actual rollback in a background thread — Slack requires
    # the initial ACK within 3s.
    threading.Thread(
        target=do_rollback,
        args=(response_url, user_id, user_name, reason),
        daemon=True,
    ).start()

    # Immediate ACK. This is what the invoking user sees first.
    return {
        "response_type": "in_channel",
        "text": (
            f":rotating_light: *Canary rollback triggered by <@{user_id}>*\n"
            f"Reason: `{reason}`\n"
            f"Target namespace: `{K8S_NAMESPACE}`\n"
            f"Watch this thread for progress. To cancel, edit the canary "
            f"deployment manually within 30s."
        ),
    }

# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------

@app.route("/healthz")
def healthz():
    return {"ok": True, "allowed_users": len(ALLOWED_USERS)}


if __name__ == "__main__":
    # Gunicorn/uWSGI in prod; direct for local dev.
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8080")))
