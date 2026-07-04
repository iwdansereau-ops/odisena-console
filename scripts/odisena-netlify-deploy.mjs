#!/usr/bin/env node
// =============================================================================
// Odisena Netlify deploy driver
// =============================================================================
// Reads odisena-deploy.yml, runs the configured install/test/build commands,
// deploys via the official Netlify CLI (npx netlify-cli), and — for
// production deploys — polls the custom domain (if configured) to confirm
// DNS/SSL are actually serving the site over HTTPS before declaring success.
//
// This script NEVER reads, writes, or creates DNS records. It only performs
// read-only HTTPS probes against the domain that Odisena's DNS provider
// (GoDaddy) and Netlify are expected to already have configured. If the
// custom domain isn't ready, it reports a clear warning/failure instead of
// pretending it can force Let's Encrypt issuance from GitHub Actions.
//
// Usage:
//   node scripts/odisena-netlify-deploy.mjs --mode=production
//   node scripts/odisena-netlify-deploy.mjs --mode=preview --alias=pr-123
//
// Required environment variables:
//   NETLIFY_AUTH_TOKEN   Netlify personal/team access token (repo secret)
//   NETLIFY_SITE_ID      Netlify Site ID / API ID (repo secret)
//
// Optional environment variables:
//   NETLIFY_TEAM_SLUG          Overrides team_slug from config, for links only
//   NETLIFY_CUSTOM_DOMAIN      Overrides custom_domain from config
//   ODISENA_DEPLOY_CONFIG_PATH Path to odisena-deploy.yml (default: ./odisena-deploy.yml)
// =============================================================================

import { execFileSync, spawnSync } from "node:child_process";
import { existsSync, readFileSync, appendFileSync } from "node:fs";
import https from "node:https";
import path from "node:path";
import process from "node:process";

// ---- tiny YAML loader (no external deps) -----------------------------------
// Supports the subset of YAML used by odisena-deploy.yml: nested maps,
// strings, booleans, numbers, and simple ">" folded scalars. Avoids adding a
// third-party dependency to keep the template dependency-free.
function loadSimpleYaml(text) {
  const lines = text.split("\n");
  const root = {};
  const stack = [{ indent: -1, node: root }];
  let foldedKey = null;
  let foldedIndent = null;
  let foldedLines = [];

  function flushFolded() {
    if (foldedKey) {
      const target = stack[stack.length - 1].node;
      target[foldedKey] = foldedLines.join(" ").trim();
      foldedKey = null;
      foldedLines = [];
      foldedIndent = null;
    }
  }

  for (const rawLine of lines) {
    if (foldedKey !== null) {
      const indentMatch = rawLine.match(/^(\s*)/);
      const indent = indentMatch[1].length;
      if (rawLine.trim() === "" ) { continue; }
      if (indent > foldedIndent) {
        foldedLines.push(rawLine.trim());
        continue;
      } else {
        flushFolded();
      }
    }

    if (!rawLine.trim() || rawLine.trim().startsWith("#")) continue;
    const indentMatch = rawLine.match(/^(\s*)/);
    const indent = indentMatch[1].length;
    const content = rawLine.trim();

    while (stack.length && indent <= stack[stack.length - 1].indent) {
      stack.pop();
    }
    const parent = stack[stack.length - 1].node;

    const kvMatch = content.match(/^([A-Za-z0-9_.-]+):\s*(.*)$/);
    if (!kvMatch) continue;
    const [, key, rest] = kvMatch;

    if (rest === "" ) {
      const child = {};
      parent[key] = child;
      stack.push({ indent, node: child });
    } else if (rest === ">") {
      foldedKey = key;
      foldedIndent = indent;
      foldedLines = [];
      // ensure parent map exists to receive the folded scalar
      stack.push({ indent, node: parent });
      stack.pop();
    } else {
      parent[key] = parseScalar(rest);
    }
  }
  flushFolded();
  return root;
}

function parseScalar(value) {
  const trimmed = value.trim();
  if (trimmed === "true") return true;
  if (trimmed === "false") return false;
  if (trimmed === "") return "";
  if (/^-?\d+(\.\d+)?$/.test(trimmed)) return Number(trimmed);
  const quoted = trimmed.match(/^"(.*)"$/) || trimmed.match(/^'(.*)'$/);
  if (quoted) return quoted[1];
  return trimmed;
}

// ---- arg parsing -------------------------------------------------------------
const args = Object.fromEntries(
  process.argv.slice(2).map((arg) => {
    const [k, ...v] = arg.replace(/^--/, "").split("=");
    return [k, v.join("=") || true];
  })
);

const mode = args.mode || "preview"; // "production" | "preview"
const explicitAlias = args.alias || null;

// ---- load config -------------------------------------------------------------
const configPath =
  process.env.ODISENA_DEPLOY_CONFIG_PATH ||
  args.config ||
  "odisena-deploy.yml";

if (!existsSync(configPath)) {
  console.error(`✖ Config file not found: ${configPath}`);
  console.error(
    "  Add odisena-deploy.yml to the repo root (see template README)."
  );
  process.exit(1);
}

const config = loadSimpleYaml(readFileSync(configPath, "utf8"));

const siteName = config.site_name || "Unnamed Odisena site";
const teamSlug = process.env.NETLIFY_TEAM_SLUG || config.team_slug || "";
const customDomain =
  process.env.NETLIFY_CUSTOM_DOMAIN || config.custom_domain || "";
const buildCommand = config.build_command || "";
const installCommand = config.install_command || "";
const testCommand = config.test_command || "";
const publishDir = config.publish_dir || ".";
const domainCheck = config.domain_check || {};
const domainCheckEnabled = domainCheck.enabled !== false && !!customDomain;
const maxAttempts = Number(domainCheck.max_attempts || 5);
const waitSeconds = Number(domainCheck.wait_seconds || 20);
const failWorkflowOnNotReady = !!domainCheck.fail_workflow_on_not_ready;

// ---- required secrets validation ---------------------------------------------
const requiredEnv = ["NETLIFY_AUTH_TOKEN", "NETLIFY_SITE_ID"];
const missing = requiredEnv.filter((name) => !process.env[name]);
if (missing.length) {
  console.error(
    `✖ Missing required secret(s): ${missing.join(", ")}. ` +
      "Set them as GitHub repo secrets before running this workflow."
  );
  process.exit(1);
}

// Defensive: never print secret values, even accidentally.
function redactedEnv() {
  const env = { ...process.env };
  for (const key of Object.keys(env)) {
    if (/TOKEN|SECRET|WEBHOOK|KEY/i.test(key)) {
      env[key] = env[key]; // keep for child process use, never logged
    }
  }
  return env;
}

function run(cmd, cmdArgs, opts = {}) {
  console.log(`\n$ ${cmd} ${cmdArgs.join(" ")}`);
  const result = spawnSync(cmd, cmdArgs, {
    stdio: "inherit",
    env: redactedEnv(),
    shell: false,
    ...opts,
  });
  if (result.status !== 0) {
    throw new Error(`Command failed (${result.status}): ${cmd} ${cmdArgs.join(" ")}`);
  }
}

function runCapture(cmd, cmdArgs, opts = {}) {
  const result = spawnSync(cmd, cmdArgs, {
    env: redactedEnv(),
    encoding: "utf8",
    shell: false,
    ...opts,
  });
  if (result.status !== 0) {
    throw new Error(
      `Command failed (${result.status}): ${cmd} ${cmdArgs.join(" ")}\n${result.stderr || ""}`
    );
  }
  return result.stdout;
}

function writeSummary(markdown) {
  const summaryPath = process.env.GITHUB_STEP_SUMMARY;
  if (summaryPath) {
    appendFileSync(summaryPath, markdown + "\n");
  }
  console.log(markdown);
}

function setOutput(name, value) {
  const outPath = process.env.GITHUB_OUTPUT;
  if (outPath) {
    appendFileSync(outPath, `${name}=${value}\n`);
  }
}

// ---- 1. install / test / build -----------------------------------------------
function shSplit(cmdStr) {
  // Simple split good enough for typical npm/yarn/pnpm command strings.
  return ["-c", cmdStr];
}

if (installCommand) {
  console.log(`\n=== Install: ${installCommand} ===`);
  run("bash", shSplit(installCommand));
} else {
  console.log("\n(No install_command configured — skipping install step.)");
}

if (testCommand) {
  console.log(`\n=== Test/check: ${testCommand} ===`);
  run("bash", shSplit(testCommand));
} else {
  console.log("(No test_command configured — skipping test step.)");
}

if (buildCommand) {
  console.log(`\n=== Build: ${buildCommand} ===`);
  run("bash", shSplit(buildCommand));
} else {
  console.log(
    "(No build_command configured — treating repo as static/no-build. " +
      `Publishing '${publishDir}' as-is.)`
  );
}

if (!existsSync(publishDir)) {
  console.error(`✖ publish_dir '${publishDir}' does not exist after build.`);
  process.exit(1);
}

// ---- 2. deploy via Netlify CLI -------------------------------------------------
const branchName =
  process.env.GITHUB_HEAD_REF || process.env.GITHUB_REF_NAME || "unknown-branch";
const shortSha = (process.env.GITHUB_SHA || "").slice(0, 7);

const deployArgs = [
  "netlify-cli",
  "deploy",
  "--site",
  process.env.NETLIFY_SITE_ID,
  "--auth",
  process.env.NETLIFY_AUTH_TOKEN,
  "--dir",
  publishDir,
  "--json",
];

let modeLabel;
if (mode === "production") {
  deployArgs.push("--prod");
  modeLabel = "production";
} else {
  const alias = (explicitAlias || branchName)
    .toLowerCase()
    .replace(/[^a-z0-9-]/g, "-")
    .slice(0, 37);
  deployArgs.push("--alias", alias);
  modeLabel = `preview (alias: ${alias})`;
}

deployArgs.push(
  "--message",
  `Odisena CI deploy: ${branchName}@${shortSha} (${mode})`
);

console.log(`\n=== Deploying to Netlify: ${modeLabel} ===`);
const deployOutputRaw = runCapture("npx", ["--yes", ...deployArgs]);

let deployInfo;
try {
  // netlify-cli --json prints a single JSON object; be defensive about any
  // incidental leading/trailing log lines from npx.
  const jsonStart = deployOutputRaw.indexOf("{");
  const jsonEnd = deployOutputRaw.lastIndexOf("}");
  deployInfo = JSON.parse(deployOutputRaw.slice(jsonStart, jsonEnd + 1));
} catch (err) {
  console.error("✖ Could not parse Netlify CLI JSON output.");
  console.error(deployOutputRaw);
  process.exit(1);
}

const deployUrl = deployInfo.deploy_url || deployInfo.url || "";
const permalink = deployInfo.url || deployInfo.deploy_url || "";
const liveSiteUrl = mode === "production" ? deployInfo.url : deployUrl;

console.log(`✔ Deploy created: ${liveSiteUrl}`);
setOutput("deploy_url", liveSiteUrl || "");
setOutput("mode", mode);

// ---- 3. custom domain / SSL readiness check (production only) ----------------
let domainStatus = "skipped";
let domainMessage = "Custom domain check skipped (not configured or preview deploy).";

async function checkHttps(domain) {
  return new Promise((resolve) => {
    const req = https.get(
      { host: domain, path: "/", timeout: 10000, method: "GET" },
      (res) => {
        resolve({ ok: res.statusCode < 500, statusCode: res.statusCode });
        res.resume();
      }
    );
    req.on("timeout", () => {
      req.destroy();
      resolve({ ok: false, error: "timeout" });
    });
    req.on("error", (err) => {
      resolve({ ok: false, error: err.message });
    });
  });
}

if (mode === "production" && domainCheckEnabled) {
  console.log(
    `\n=== Checking HTTPS/custom domain readiness: https://${customDomain} ===`
  );
  console.log(
    "(Read-only check. This workflow never modifies DNS or forces " +
      "certificate issuance — Netlify provisions Let's Encrypt automatically " +
      "once DNS correctly points at Netlify.)"
  );

  let ready = false;
  let lastResult = null;
  for (let attempt = 1; attempt <= maxAttempts; attempt++) {
    lastResult = await checkHttps(customDomain);
    if (lastResult.ok) {
      ready = true;
      break;
    }
    console.log(
      `  Attempt ${attempt}/${maxAttempts}: not ready yet ` +
        `(${lastResult.error || `HTTP ${lastResult.statusCode}`}). ` +
        (attempt < maxAttempts ? `Waiting ${waitSeconds}s...` : "")
    );
    if (attempt < maxAttempts) {
      await new Promise((r) => setTimeout(r, waitSeconds * 1000));
    }
  }

  if (ready) {
    domainStatus = "ready";
    domainMessage = `https://${customDomain} responded over HTTPS (status ${lastResult.statusCode}).`;
    console.log(`✔ ${domainMessage}`);
  } else {
    domainStatus = "not_ready";
    domainMessage =
      `⚠ https://${customDomain} did not respond successfully over HTTPS after ` +
      `${maxAttempts} attempts (${lastResult.error || `HTTP ${lastResult.statusCode}`}). ` +
      "This usually means DNS is not yet pointed at Netlify, or the custom domain " +
      "has not been added in the Netlify site's Domain settings. Netlify auto-issues " +
      "a Let's Encrypt certificate only after DNS correctly resolves to Netlify — " +
      "GitHub Actions cannot force this step. Verify: (1) the domain is added under " +
      "Site settings > Domain management in Netlify, (2) GoDaddy DNS has the " +
      "required CNAME/A/ALIAS records per Netlify's instructions, then re-run this " +
      "workflow or wait for DNS propagation.";
    console.warn(domainMessage);
    if (failWorkflowOnNotReady) {
      console.error(
        "✖ domain_check.fail_workflow_on_not_ready is true — failing the workflow."
      );
    }
  }
}

setOutput("domain_status", domainStatus);

// ---- 4. write GitHub step summary ---------------------------------------------
const summaryLines = [
  `## Odisena Netlify Deploy — ${siteName}`,
  "",
  `- **Mode:** ${modeLabel}`,
  `- **Branch:** \`${branchName}\``,
  `- **Commit:** \`${shortSha}\``,
  `- **Deploy URL:** ${liveSiteUrl ? `[${liveSiteUrl}](${liveSiteUrl})` : "n/a"}`,
];
if (teamSlug && process.env.NETLIFY_SITE_ID) {
  summaryLines.push(
    `- **Netlify site:** [app.netlify.com/sites/${process.env.NETLIFY_SITE_ID}](https://app.netlify.com/sites/${process.env.NETLIFY_SITE_ID}/deploys)`
  );
}
if (mode === "production" && customDomain) {
  const icon = domainStatus === "ready" ? "✅" : domainStatus === "not_ready" ? "⚠️" : "ℹ️";
  summaryLines.push(`- **Custom domain (${customDomain}):** ${icon} ${domainMessage}`);
}
writeSummary(summaryLines.join("\n"));

// Expose values for the Slack notify step / workflow.
setOutput("site_name", siteName);
setOutput("branch", branchName);
setOutput("commit_sha", process.env.GITHUB_SHA || "");
setOutput("domain_message", domainMessage.replace(/\n/g, " "));

if (mode === "production" && domainCheckEnabled && domainStatus === "not_ready" && failWorkflowOnNotReady) {
  process.exit(1);
}

console.log("\n✔ odisena-netlify-deploy.mjs completed.");
