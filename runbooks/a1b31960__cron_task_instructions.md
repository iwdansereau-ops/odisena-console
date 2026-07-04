# Background cron task instructions — hourly canary soak check

You are a background agent triggered hourly during a 24-hour OTel Collector canary soak.

## Steps to perform each fire

1. **Read config** from `/home/user/workspace/otel_canary/state/soak_config.json`.
   - Fields: `prometheus_url`, `prometheus_bearer`, `grafana_dashboard_url`, `slack_channel_id`, `soak_start_iso`, `total_hours`.

2. **Track fire count.** Look for `/home/user/workspace/otel_canary/state/fire_count.txt`. If missing, create with `1`. Otherwise read, increment, write back. Call this `N`.

3. **Self-terminate check.** If `N > total_hours` (i.e., N > 24), delete this scheduled task (schedule_cron action=delete with the current cron_id from the task header) and send a final Slack DM: `":checkered_flag: OTel canary soak complete — 24h monitoring window ended. Task deleted."` Then stop.

4. **Bootstrap check.** If `prometheus_url` is empty:
   - Send a Slack DM to `slack_channel_id`:
     ```
     :hourglass_flowing_sand: *OTel canary soak — hour N/24 (bootstrap)*
     Prometheus URL not configured yet. Reply in the main thread with:
       • Prometheus base URL (e.g., https://prom.internal.company.com)
       • Bearer token (optional)
       • Grafana dashboard URL (optional)
     I'll update /home/user/workspace/otel_canary/state/soak_config.json and the next fire will run the real check.
     ```
   - Stop this fire. Do not query Prometheus.

5. **Run the check.** Execute:
   ```bash
   PROMETHEUS_URL="<url>" \
   PROMETHEUS_BEARER="<token or empty>" \
   GRAFANA_DASHBOARD_URL="<url>" \
   SLACK_CHANNEL_ID="<id>" \
   SOAK_START_ISO="<iso>" \
   python3 /home/user/workspace/otel_canary/scripts/hourly_check.py
   ```
   The script prints JSON to stdout with a `slack_text` field ready for Slack.

6. **Post to Slack.** Parse the JSON, extract `slack_text`, call `slack_send_message` with `channel_id=<slack_channel_id>` and `text=<slack_text>`.

7. **Log the result** to `/home/user/workspace/cron_tracking/{cron_id}/fire_N.json` (full script output for audit).

## Failure modes

- **Script exit 2 (config error):** Post a Slack DM saying config is broken. Do not delete the cron.
- **Script exit 3 (Prometheus error):** Post a Slack DM with the error message. Continue running (transient network issues shouldn't kill the monitor).
- **Slack API failure:** Log locally, don't crash.

## Do NOT

- Do NOT ask the user clarifying questions during a background run — just DM Slack.
- Do NOT re-schedule; there's already one cron and it self-terminates.
- Do NOT modify `hourly_check.py` — it's validated.
