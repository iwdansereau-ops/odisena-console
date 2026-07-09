#!/usr/bin/env node
// =============================================================================
// Odisena Slack deploy notifier
// =============================================================================
// Posts a message to the Odisena deployments Slack channel once a deploy
// finishes (success or failure). Uses a generic Slack Incoming Webhook URL
// stored in the SLACK_WEBHOOK_URL secret — no bot token or app install is
// required, though the script also supports a bot-token + chat.postMessage
// path if SLACK_BOT_TOKEN and SLACK_CHANNEL_ID are provided instead.
//
// This script never hardcodes a webhook URL or token. If neither
// SLACK_WEBHOOK_URL nor (SLACK_BOT_TOKEN + SLACK_CHANNEL_ID) is set, it
// prints a warning and exits successfully (0) so a missing/optional Slack
// integration never fails the deploy pipeline itself.
//
// Usage:
//   node scripts/odisena-slack-notify.mjs \
//     --status=success \
//     --mode=production \
//     --site-name="Odisena Console" \
//     --deploy-url="https://console.odisena.com" \
//     --domain-message="..." \
//     --repo="iwdansereau-ops/odisena-console" \
//     --branch=main \
//     --commit=abcdef1
// =============================================================================

import https from "node:https";
import process from "node:process";

const args = Object.fromEntries(
  process.argv.slice(2).map((arg) => {
    const [k, ...v] = arg.replace(/^--/, "").split("=");
    return [k, v.join("=") || true];
  })
);

const status = (args.status || "unknown").toLowerCase(); // success | failure
const mode = args.mode || "preview"; // production | preview
const siteName = args["site-name"] || "Odisena site";
const deployUrl = args["deploy-url"] || "";
const domainMessage = args["domain-message"] || "";
const repo = args.repo || process.env.GITHUB_REPOSITORY || "";
const branch = args.branch || "";
const commit = args.commit || "";
const netlifySiteId = process.env.NETLIFY_SITE_ID || "";
const runUrl =
  process.env.GITHUB_SERVER_URL &&
  process.env.GITHUB_REPOSITORY &&
  process.env.GITHUB_RUN_ID
    ? `${process.env.GITHUB_SERVER_URL}/${process.env.GITHUB_REPOSITORY}/actions/runs/${process.env.GITHUB_RUN_ID}`
    : "";

const webhookUrl = process.env.SLACK_WEBHOOK_URL || "";
const botToken = process.env.SLACK_BOT_TOKEN || "";
const channelId = process.env.SLACK_CHANNEL_ID || "";

if (!webhookUrl && !(botToken && channelId)) {
  console.warn(
    "⚠ No Slack credentials configured (SLACK_WEBHOOK_URL, or " +
      "SLACK_BOT_TOKEN + SLACK_CHANNEL_ID). Skipping Slack notification. " +
      "This is non-fatal."
  );
  process.exit(0);
}

const statusEmoji = status === "success" ? "✅" : status === "failure" ? "❌" : "ℹ️";
const modeLabel = mode === "production" ? "Production" : "Preview";

const lines = [
  `${statusEmoji} *${modeLabel} deploy ${status === "success" ? "succeeded" : status}* — ${siteName}`,
  repo ? `*Repo:* ${repo}` : null,
  branch ? `*Branch:* \`${branch}\`` : null,
  commit ? `*Commit:* \`${commit.slice(0, 7)}\`` : null,
  netlifySiteId ? `*Netlify Site ID:* \`${netlifySiteId}\`` : null,
  deployUrl ? `*Deploy URL:* ${deployUrl}` : null,
  domainMessage ? `*Domain status:* ${domainMessage}` : null,
  runUrl ? `*Workflow run:* ${runUrl}` : null,
].filter(Boolean);

const text = lines.join("\n");

function postJson(urlString, headers, body) {
  return new Promise((resolve, reject) => {
    const url = new URL(urlString);
    const payload = JSON.stringify(body);
    const req = https.request(
      {
        hostname: url.hostname,
        path: url.pathname + url.search,
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "Content-Length": Buffer.byteLength(payload),
          ...headers,
        },
        timeout: 10000,
      },
      (res) => {
        let data = "";
        res.on("data", (chunk) => (data += chunk));
        res.on("end", () =>
          resolve({ statusCode: res.statusCode, body: data })
        );
      }
    );
    req.on("timeout", () => {
      req.destroy();
      reject(new Error("Slack request timed out"));
    });
    req.on("error", reject);
    req.write(payload);
    req.end();
  });
}

async function main() {
  try {
    if (webhookUrl) {
      const result = await postJson(webhookUrl, {}, { text });
      if (result.statusCode >= 200 && result.statusCode < 300) {
        console.log("✔ Slack notification sent via incoming webhook.");
      } else {
        console.warn(
          `⚠ Slack webhook returned HTTP ${result.statusCode}: ${result.body}`
        );
      }
    } else {
      const result = await postJson(
        "https://slack.com/api/chat.postMessage",
        { Authorization: `Bearer ${botToken}` },
        { channel: channelId, text }
      );
      const parsed = JSON.parse(result.body || "{}");
      if (parsed.ok) {
        console.log("✔ Slack notification sent via bot token.");
      } else {
        console.warn(`⚠ Slack API error: ${parsed.error || result.body}`);
      }
    }
  } catch (err) {
    // Never fail the pipeline because Slack notification failed.
    console.warn(`⚠ Slack notification failed: ${err.message}`);
  }
}

await main();
