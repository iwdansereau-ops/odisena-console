#!/usr/bin/env python3
"""
Append one row to the "DB Migration Preflight Runs" Notion database per
workflow execution. Called by the db-migration-preflight GitHub Action after
the preflight scripts run.

Uses the native Notion REST API (POST /v1/pages) so this works from any
CI runner with just a Notion internal integration token — no MCP, no
extra services. Failures are non-fatal by design: logging is telemetry,
not a gate on the build. The script exits 0 even on Notion API errors
(with a warning to the job log) so a Notion outage never blocks a merge.

Required env vars:
  NOTION_TOKEN            secret_... internal-integration token
  NOTION_DATABASE_ID      target database ID (with or without dashes)

Required CLI args cover everything the workflow already has in scope:
  --pr-number, --pr-title, --pr-url, --repo, --branch, --commit-sha,
  --config-exit, --hygiene-exit, --config-status, --hygiene-status,
  --overall-status, --exec-seconds, --actor, --event, --run-url,
  --migration-files (path to a file containing one filename per line;
                     may be empty for workflow_dispatch runs).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

NOTION_API_URL = "https://api.notion.com/v1/pages"
NOTION_VERSION = "2022-06-28"  # stable; page-create schema hasn't changed

# Rich-text has a 2000-char per-chunk limit. Migration file lists past this
# get truncated with a trailing "+N more" marker so the row still writes.
MAX_RICH_TEXT_CHARS = 1900


def _rich_text(value: str) -> list[dict]:
    """Wrap a string as a Notion rich_text property, truncating if needed."""
    if value is None:
        return []
    text = str(value)
    if len(text) > MAX_RICH_TEXT_CHARS:
        text = text[: MAX_RICH_TEXT_CHARS - 20] + "\n… (truncated)"
    return [{"type": "text", "text": {"content": text}}]


def _select(name: str | None) -> dict | None:
    if not name:
        return None
    return {"name": name}


def _number(value) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _url(value: str | None) -> str | None:
    if not value:
        return None
    return value


def _read_migration_files(path: str | None) -> list[str]:
    if not path:
        return []
    p = Path(path)
    if not p.exists():
        return []
    lines = [line.strip() for line in p.read_text().splitlines()]
    return [line for line in lines if line]


def build_properties(args: argparse.Namespace) -> dict:
    """Assemble the Notion page-properties payload for a single run row."""
    mig_files = _read_migration_files(args.migration_files)
    mig_files_text = "\n".join(mig_files) if mig_files else "(no db/migrations/** files changed)"

    # Row title: human-scannable identifier — repo, PR, run.
    if args.pr_number:
        title = f"{args.repo} · PR #{args.pr_number} · run {args.run_id or ''}".strip(" ·")
    else:
        title = f"{args.repo} · {args.event or 'run'} · run {args.run_id or ''}".strip(" ·")

    props: dict = {
        "Run": {"title": _rich_text(title)},
        "Repository": {"rich_text": _rich_text(args.repo)},
        "Branch": {"rich_text": _rich_text(args.branch or "")},
        "Commit SHA": {"rich_text": _rich_text((args.commit_sha or "")[:12])},
        "Overall Status": {"select": _select(args.overall_status)},
        "Config Verifier": {"select": _select(args.config_status)},
        "Session Hygiene": {"select": _select(args.hygiene_status)},
        "Migration Files": {"rich_text": _rich_text(mig_files_text)},
        "Migration File Count": {"number": len(mig_files)},
        "Actions Run URL": {"url": _url(args.run_url)},
        "Triggered By": {"rich_text": _rich_text(args.actor or "")},
        "Event": {"select": _select(args.event)},
        "Run Time": {
            "date": {"start": datetime.now(timezone.utc).isoformat(timespec="seconds")}
        },
    }

    # Optional numeric fields — omit the property entirely if we don't have
    # a real value (rather than pushing null, which some Notion clients
    # render as an empty cell but count as a set value).
    pr_num = _number(args.pr_number)
    if pr_num is not None:
        props["PR Number"] = {"number": pr_num}
    if args.pr_title:
        props["PR Title"] = {"rich_text": _rich_text(args.pr_title)}
    if args.pr_url:
        props["PR URL"] = {"url": args.pr_url}
    cfg_exit = _number(args.config_exit)
    if cfg_exit is not None:
        props["Config Exit Code"] = {"number": cfg_exit}
    hyg_exit = _number(args.hygiene_exit)
    if hyg_exit is not None:
        props["Hygiene Exit Code"] = {"number": hyg_exit}
    exec_sec = _number(args.exec_seconds)
    if exec_sec is not None:
        props["Execution Time (s)"] = {"number": exec_sec}

    return props


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--pr-number", default="")
    p.add_argument("--pr-title", default="")
    p.add_argument("--pr-url", default="")
    p.add_argument("--repo", required=True)
    p.add_argument("--branch", default="")
    p.add_argument("--commit-sha", default="")
    p.add_argument("--config-exit", default="")
    p.add_argument("--hygiene-exit", default="")
    p.add_argument("--config-status", choices=["PASS", "FAIL", "ERROR", ""], default="")
    p.add_argument("--hygiene-status", choices=["PASS", "FAIL", "ERROR", ""], default="")
    p.add_argument(
        "--overall-status", choices=["PASS", "BLOCKED", "ERROR", ""], default=""
    )
    p.add_argument("--exec-seconds", default="")
    p.add_argument("--actor", default="")
    p.add_argument("--event", default="")
    p.add_argument("--run-url", default="")
    p.add_argument("--run-id", default="")
    p.add_argument(
        "--migration-files",
        default="",
        help="Path to a text file with one migration filename per line.",
    )
    args = p.parse_args()

    token = os.environ.get("NOTION_TOKEN", "").strip()
    db_id = os.environ.get("NOTION_DATABASE_ID", "").strip()
    if not token or not db_id:
        # No creds → silently skip. The workflow uses `continue-on-error` to
        # skip the step when secrets are missing, but this belt-and-suspenders
        # check protects against a partial secret rollout.
        print(
            "log_preflight_to_notion: NOTION_TOKEN or NOTION_DATABASE_ID unset; "
            "skipping Notion log.",
            file=sys.stderr,
        )
        return 0

    payload = {
        "parent": {"database_id": db_id},
        "properties": build_properties(args),
    }

    req = urllib.request.Request(
        NOTION_API_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {token}",
            "Notion-Version": NOTION_VERSION,
            "Content-Type": "application/json",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            data = json.loads(body)
            page_url = data.get("url", "(no url returned)")
            print(f"log_preflight_to_notion: logged run to {page_url}")
            return 0
    except urllib.error.HTTPError as e:
        err_body = e.read().decode("utf-8", errors="replace") if e.fp else ""
        print(
            f"::warning title=Notion log failed::HTTP {e.code}: {err_body[:500]}",
            file=sys.stderr,
        )
        # Never block the build on a Notion outage.
        return 0
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        print(
            f"::warning title=Notion log failed::Network error contacting Notion: {e}",
            file=sys.stderr,
        )
        return 0


if __name__ == "__main__":
    sys.exit(main())
