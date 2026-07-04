#!/usr/bin/env python3
"""
format_preflight_output.py

Converts the raw output of verify_rds_migration_config.sh and
preflight_session_hygiene.sh into three artifacts:

  1. `--format plain`    — ANSI codes stripped, suitable for CI logs.
  2. `--format markdown` — GitHub-flavored Markdown for PR comments.
  3. `--format slack`    — Slack mrkdwn payload (JSON) for Incoming Webhooks.

The scripts already emit PASS / WARN / FAIL / INFO tokens. This helper:

  * strips ANSI escape sequences (colors are lost in Markdown/Slack anyway)
  * prefixes those tokens with color-preserving emoji so the semantic color
    survives the strip:  PASS -> 🟢 PASS   WARN -> 🟡 WARN   FAIL -> 🔴 FAIL
  * wraps the whole report in a fenced code block so column alignment is
    preserved in both GitHub and Slack renderers

Usage:
  cat script_output.txt | ./format_preflight_output.py \
      --format markdown \
      --config-exit 0 --hygiene-exit 1 \
      --pr-url https://github.com/org/repo/pull/123 \
      --commit-sha abcdef0
"""

import argparse
import json
import re
import sys
from typing import Tuple

# ANSI escape sequence pattern (CSI + parameters + final byte).
# Covers the SGR codes emitted by the scripts (\033[1m, \033[31m, \033[0m, ...).
_ANSI_RE = re.compile(r"\x1B\[[0-?]*[ -/]*[@-~]")

# Match the status token as a standalone word, so we don't rewrite the word
# "PASS" inside a query string. Requires whitespace or start-of-line before
# and whitespace or end-of-line after. Anchored *after* ANSI stripping.
_STATUS_RE = re.compile(r"(^|[\s])(PASS|WARN|FAIL|INFO)(\s|$)")

_STATUS_EMOJI = {
    "PASS": "🟢",
    "WARN": "🟡",
    "FAIL": "🔴",
    "INFO": "🔵",
}


def strip_ansi(text: str) -> str:
    """Remove all ANSI CSI sequences."""
    return _ANSI_RE.sub("", text)


def annotate_statuses(text: str) -> str:
    """Prefix PASS/WARN/FAIL/INFO tokens with their color emoji."""
    def repl(m: re.Match) -> str:
        pre, tok, post = m.group(1), m.group(2), m.group(3)
        return f"{pre}{_STATUS_EMOJI[tok]} {tok}{post}"
    return _STATUS_RE.sub(repl, text)


def overall_status(config_exit: int, hygiene_exit: int) -> Tuple[str, str]:
    """Return (emoji, short label) for the summary."""
    if config_exit == 0 and hygiene_exit == 0:
        return "✅", "READY TO MIGRATE"
    return "❌", "MIGRATION BLOCKED"


def script_status(exit_code: int) -> str:
    if exit_code == 0:
        return "🟢 PASS"
    if exit_code == 1:
        return "🔴 FAIL"
    return f"⚠️ ERROR (exit {exit_code})"


def build_plain(config_out: str, hygiene_out: str) -> str:
    return (
        "===== Config Verifier =====\n"
        + strip_ansi(config_out).rstrip()
        + "\n\n===== Session Hygiene =====\n"
        + strip_ansi(hygiene_out).rstrip()
        + "\n"
    )


def build_markdown(
    config_out: str,
    hygiene_out: str,
    config_exit: int,
    hygiene_exit: int,
    pr_url: str,
    commit_sha: str,
    run_url: str,
) -> str:
    emoji, label = overall_status(config_exit, hygiene_exit)

    def block(title: str, exit_code: int, raw: str) -> str:
        body = annotate_statuses(strip_ansi(raw)).rstrip()
        # Fence with ``` and language `text` so GitHub keeps column alignment
        # and does not attempt syntax highlighting.
        return (
            f"<details open><summary><strong>{title}</strong> — {script_status(exit_code)}"
            f"</summary>\n\n```text\n{body}\n```\n\n</details>"
        )

    parts = [
        f"### {emoji} Database Migration Preflight: **{label}**",
        "",
        "| Check | Result |",
        "| --- | --- |",
        f"| Config verifier (`verify_rds_migration_config.sh`) | {script_status(config_exit)} |",
        f"| Session hygiene (`preflight_session_hygiene.sh`)  | {script_status(hygiene_exit)} |",
        "",
        block("Config Verifier Output", config_exit, config_out),
        block("Session Hygiene Output", hygiene_exit, hygiene_out),
        "",
        f"<sub>Commit `{commit_sha[:7]}` · [Workflow run]({run_url}) · [PR]({pr_url})</sub>",
        "",
        "<!-- migration-preflight-marker -->",
    ]
    return "\n".join(parts)


def build_slack(
    config_out: str,
    hygiene_out: str,
    config_exit: int,
    hygiene_exit: int,
    pr_url: str,
    pr_title: str,
    repo: str,
    commit_sha: str,
    run_url: str,
) -> dict:
    """
    Build a Slack Block Kit payload. Uses mrkdwn for the tables inside code
    fences so column alignment survives.

    Slack's mrkdwn code block max is generous (~40 KB per block, 3000 chars per
    text object). We truncate defensively.
    """
    emoji, label = overall_status(config_exit, hygiene_exit)

    def slack_block(title: str, exit_code: int, raw: str) -> dict:
        body = annotate_statuses(strip_ansi(raw)).rstrip()
        # 2900 leaves headroom for the surrounding fence and title
        if len(body) > 2900:
            body = body[:2900] + "\n… (truncated, see PR comment for full output)"
        return {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*{title}* — {script_status(exit_code)}\n```\n{body}\n```",
            },
        }

    header_text = f"{emoji} Migration Preflight: {label}"
    context_text = (
        f"<{pr_url}|{pr_title}> in `{repo}` · commit `{commit_sha[:7]}` · "
        f"<{run_url}|workflow run>"
    )

    return {
        "blocks": [
            {
                "type": "header",
                "text": {"type": "plain_text", "text": header_text, "emoji": True},
            },
            {
                "type": "section",
                "fields": [
                    {"type": "mrkdwn", "text": f"*Config verifier*\n{script_status(config_exit)}"},
                    {"type": "mrkdwn", "text": f"*Session hygiene*\n{script_status(hygiene_exit)}"},
                ],
            },
            {"type": "divider"},
            slack_block("Config Verifier", config_exit, config_out),
            slack_block("Session Hygiene", hygiene_exit, hygiene_out),
            {
                "type": "context",
                "elements": [{"type": "mrkdwn", "text": context_text}],
            },
        ]
    }


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--format", required=True, choices=["plain", "markdown", "slack"])
    p.add_argument("--config-output", required=True, help="Path to config verifier output file")
    p.add_argument("--hygiene-output", required=True, help="Path to session hygiene output file")
    p.add_argument("--config-exit", type=int, required=True)
    p.add_argument("--hygiene-exit", type=int, required=True)
    p.add_argument("--pr-url", default="")
    p.add_argument("--pr-title", default="Database migration PR")
    p.add_argument("--repo", default="")
    p.add_argument("--commit-sha", default="")
    p.add_argument("--run-url", default="")
    args = p.parse_args()

    with open(args.config_output, encoding="utf-8", errors="replace") as f:
        config_out = f.read()
    with open(args.hygiene_output, encoding="utf-8", errors="replace") as f:
        hygiene_out = f.read()

    if args.format == "plain":
        sys.stdout.write(build_plain(config_out, hygiene_out))
    elif args.format == "markdown":
        sys.stdout.write(
            build_markdown(
                config_out, hygiene_out,
                args.config_exit, args.hygiene_exit,
                args.pr_url, args.commit_sha, args.run_url,
            )
        )
    else:  # slack
        payload = build_slack(
            config_out, hygiene_out,
            args.config_exit, args.hygiene_exit,
            args.pr_url, args.pr_title, args.repo,
            args.commit_sha, args.run_url,
        )
        sys.stdout.write(json.dumps(payload, ensure_ascii=False))

    return 0


if __name__ == "__main__":
    sys.exit(main())
