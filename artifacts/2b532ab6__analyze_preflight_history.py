#!/usr/bin/env python3
"""
Correlation analyzer for the "DB Migration Preflight Runs" Notion tracker.

Pulls every historical row from the tracker, computes per-migration-file
failure rates, classifies each file against a catalog of DDL anti-patterns
known to cause the failures our preflight scripts gate on (long-running
transactions, idle-in-transaction sessions, lock-wait contention, connection
exhaustion, and out-of-spec server configuration), and prints a report
identifying the top 3 persistent offenders and the schema patterns behind them.

The report is designed to be useful with as few as a handful of runs. For
small samples, per-file failure rates are shown alongside their Wilson score
95% lower bound so a single 1/1 failure isn't reported as "100% failure rate".
Files are ranked by that lower bound, not raw percentage.

Usage:
    export NOTION_TOKEN=secret_...
    export NOTION_DATABASE_ID=42d763db-6867-4d56-93c7-eae4e2928a31
    python3 analyze_preflight_history.py

Optional flags:
    --repo-path /path/to/repo/root
        If provided, the script reads the SQL text of each migration file
        from disk and runs content-based anti-pattern detection in addition
        to filename inference. Without this flag, detection is filename-only.
    --min-runs N       (default 1) filter out files touched fewer than N times
    --top N            (default 3) how many offenders to list
    --since YYYY-MM-DD  restrict to runs on/after this date
    --format md|text   (default text) output format
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
import urllib.error
import urllib.request
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Iterable

NOTION_API_BASE = "https://api.notion.com/v1"
NOTION_VERSION = "2022-06-28"


# ---------------------------------------------------------------------------
# Notion REST client
# ---------------------------------------------------------------------------

def _notion_request(path: str, token: str, payload: dict | None = None) -> dict:
    url = f"{NOTION_API_BASE}{path}"
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(
        url,
        data=data,
        headers={
            "Authorization": f"Bearer {token}",
            "Notion-Version": NOTION_VERSION,
            "Content-Type": "application/json",
        },
        method="POST" if payload is not None else "GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace") if e.fp else ""
        raise SystemExit(f"Notion API error {e.code} on {path}: {body[:600]}")


def fetch_all_rows(token: str, database_id: str, since_iso: str | None) -> list[dict]:
    """Fetch every row in the database, paginating through the query endpoint."""
    rows: list[dict] = []
    cursor: str | None = None
    while True:
        payload: dict = {"page_size": 100}
        if cursor:
            payload["start_cursor"] = cursor
        if since_iso:
            payload["filter"] = {
                "property": "Run Time",
                "date": {"on_or_after": since_iso},
            }
        payload["sorts"] = [
            {"property": "Run Time", "direction": "descending"}
        ]
        resp = _notion_request(
            f"/databases/{database_id}/query", token, payload=payload
        )
        rows.extend(resp.get("results", []))
        if not resp.get("has_more"):
            break
        cursor = resp.get("next_cursor")
    return rows


# ---------------------------------------------------------------------------
# Row parsing
# ---------------------------------------------------------------------------

@dataclass
class Run:
    row_id: str
    pr_number: int | None
    repo: str
    overall_status: str  # PASS / BLOCKED / ERROR / ""
    config_status: str
    hygiene_status: str
    migration_files: list[str]
    run_time: str | None
    run_url: str | None


def _text(prop: dict) -> str:
    """Concat a rich_text or title property into a plain string."""
    if not prop:
        return ""
    if "title" in prop:
        return "".join(x.get("plain_text", "") for x in prop["title"])
    if "rich_text" in prop:
        return "".join(x.get("plain_text", "") for x in prop["rich_text"])
    return ""


def _select(prop: dict) -> str:
    if not prop:
        return ""
    sel = prop.get("select")
    return sel.get("name", "") if sel else ""


def _number(prop: dict):
    return prop.get("number") if prop else None


def _url(prop: dict) -> str | None:
    return prop.get("url") if prop else None


def _date(prop: dict) -> str | None:
    if not prop:
        return None
    d = prop.get("date")
    return d.get("start") if d else None


def parse_row(row: dict) -> Run:
    p = row.get("properties", {})
    files_blob = _text(p.get("Migration Files", {})).strip()
    files: list[str] = []
    if files_blob and files_blob != "(no db/migrations/** files changed)":
        files = [line.strip() for line in files_blob.splitlines() if line.strip()]
    return Run(
        row_id=row.get("id", ""),
        pr_number=int(_number(p.get("PR Number", {})) or 0) or None,
        repo=_text(p.get("Repository", {})),
        overall_status=_select(p.get("Overall Status", {})),
        config_status=_select(p.get("Config Verifier", {})),
        hygiene_status=_select(p.get("Session Hygiene", {})),
        migration_files=files,
        run_time=_date(p.get("Run Time", {})),
        run_url=_url(p.get("Actions Run URL", {})),
    )


# ---------------------------------------------------------------------------
# DDL anti-pattern catalog
# ---------------------------------------------------------------------------
#
# Each rule has:
#   name        Short human-readable label
#   why         Why this pattern causes the preflight failures we care about
#   filename    Regexes matched against the base filename (case-insensitive)
#   content     Regexes matched against SQL contents (case-insensitive, DOTALL)
#   trigger     Which preflight check(s) this pattern is most likely to trip:
#               "hygiene" (long-running / lock / idle-in-txn / connection),
#               "config"  (server-level tuning), or "either".

@dataclass
class AntiPattern:
    name: str
    why: str
    trigger: str
    filename: list[re.Pattern] = field(default_factory=list)
    content: list[re.Pattern] = field(default_factory=list)


def _rx(*patterns: str) -> list[re.Pattern]:
    return [re.compile(p, re.IGNORECASE | re.DOTALL) for p in patterns]


ANTI_PATTERNS: list[AntiPattern] = [
    AntiPattern(
        name="Blocking CREATE INDEX (missing CONCURRENTLY)",
        why=(
            "A non-concurrent CREATE INDEX takes an ACCESS EXCLUSIVE-adjacent lock "
            "that blocks writes for the duration of the build. On a large table this "
            "manifests as a lock-wait storm in pg_stat_activity and trips the "
            "session hygiene 'lock waits' check."
        ),
        trigger="hygiene",
        filename=_rx(r"add[_-]?index", r"create[_-]?index", r"_idx[._-]"),
        content=_rx(r"create\s+index\s+(?!concurrently)(?!if\s+not\s+exists\s+concurrently)"),
    ),
    AntiPattern(
        name="ALTER TABLE ... ADD COLUMN NOT NULL DEFAULT (non-constant)",
        why=(
            "Adding a NOT NULL column with a volatile or non-constant DEFAULT forces "
            "a full table rewrite under ACCESS EXCLUSIVE, which shows up as a single "
            "long-running transaction plus a large blocker/waiter tree in pg_locks. "
            "Trips both the long-running-transaction and lock-waits gates."
        ),
        trigger="hygiene",
        filename=_rx(r"add[_-]?column", r"add[_-]?not[_-]?null"),
        content=_rx(
            r"alter\s+table[^;]{0,200}\badd\s+column\b[^;]{0,300}\bnot\s+null\b[^;]{0,200}\bdefault\b"
        ),
    ),
    AntiPattern(
        name="ALTER TABLE ... ADD FOREIGN KEY without NOT VALID",
        why=(
            "A validating ADD FOREIGN KEY takes a SHARE ROW EXCLUSIVE lock on the "
            "referenced table and scans it fully. On any sizeable table this becomes "
            "a long-running transaction that holds locks blocking DML. The safe "
            "pattern is ADD ... NOT VALID + a separate VALIDATE CONSTRAINT."
        ),
        trigger="hygiene",
        filename=_rx(r"add[_-]?fk", r"add[_-]?foreign[_-]?key", r"_fk[._-]"),
        content=_rx(
            r"add\s+(constraint\s+\w+\s+)?foreign\s+key\b(?![^;]*not\s+valid)"
        ),
    ),
    AntiPattern(
        name="Single-statement bulk UPDATE / backfill",
        why=(
            "Rewriting millions of rows in one UPDATE holds row-level locks and WAL "
            "space for the life of the statement. It's the classic idle-in-transaction "
            "and long-running-transaction offender, and it inflates WAL beyond "
            "max_wal_size which is why the config verifier's WAL check often also "
            "fails on the same PR."
        ),
        trigger="either",
        filename=_rx(r"backfill", r"bulk[_-]?update", r"populate", r"migrate[_-]?data"),
        content=_rx(r"update\s+\w+[^;]{0,300};", r"insert\s+into\s+\w+\s+select\b"),
    ),
    AntiPattern(
        name="ALTER TYPE ... ADD VALUE inside a transaction",
        why=(
            "Postgres < 12 disallows ALTER TYPE ADD VALUE in a transaction block; "
            "even on 12+, wrapping it with other DDL in one txn extends the txn "
            "duration and can leave the enum in a partially-committed state visible "
            "to concurrent sessions. Shows as long-running-transaction plus flapping."
        ),
        trigger="hygiene",
        filename=_rx(r"alter[_-]?type", r"add[_-]?enum", r"enum[_-]?value"),
        content=_rx(r"alter\s+type\s+\w+\s+add\s+value\b"),
    ),
    AntiPattern(
        name="ALTER COLUMN TYPE (implicit rewrite)",
        why=(
            "Changing a column's type generally forces a full table rewrite under "
            "ACCESS EXCLUSIVE unless the conversion is binary-compatible. This is a "
            "reliable long-running-transaction + lock-waits generator."
        ),
        trigger="hygiene",
        filename=_rx(r"alter[_-]?column[_-]?type", r"change[_-]?type", r"retype"),
        content=_rx(r"alter\s+table[^;]{0,200}alter\s+column[^;]{0,200}\btype\b"),
    ),
    AntiPattern(
        name="DROP COLUMN on hot table",
        why=(
            "DROP COLUMN itself is O(1) metadata but takes ACCESS EXCLUSIVE briefly. "
            "The real problem is application-side: clients still SELECT the column "
            "and error, causing retry storms that saturate max_connections. Trips "
            "the connection-utilization check."
        ),
        trigger="hygiene",
        filename=_rx(r"drop[_-]?column", r"remove[_-]?column"),
        content=_rx(r"alter\s+table[^;]{0,200}drop\s+column\b"),
    ),
    AntiPattern(
        name="Explicit LOCK TABLE",
        why=(
            "Any explicit LOCK TABLE holds its lock until the transaction ends. "
            "In a migration this is almost always ACCESS EXCLUSIVE and blocks all "
            "readers/writers on that table — direct lock-wait failure."
        ),
        trigger="hygiene",
        filename=_rx(r"lock[_-]?table"),
        content=_rx(r"\block\s+table\b"),
    ),
    AntiPattern(
        name="VACUUM FULL / CLUSTER",
        why=(
            "VACUUM FULL and CLUSTER take ACCESS EXCLUSIVE for the entire operation "
            "and rewrite the table. On large tables this is the single worst "
            "long-running-transaction offender. Should be a scheduled maintenance "
            "task, not a migration."
        ),
        trigger="hygiene",
        filename=_rx(r"vacuum[_-]?full", r"cluster"),
        content=_rx(r"vacuum\s+full\b", r"^\s*cluster\s+"),
    ),
    AntiPattern(
        name="REINDEX (non-concurrent)",
        why=(
            "Plain REINDEX takes ACCESS EXCLUSIVE on the index and blocks writes to "
            "the table. Use REINDEX CONCURRENTLY (PG 12+) instead."
        ),
        trigger="hygiene",
        filename=_rx(r"reindex"),
        content=_rx(r"reindex\s+(?!concurrently)"),
    ),
]


def classify_file(
    filename: str, sql_text: str | None
) -> list[AntiPattern]:
    """Return every AntiPattern that matches the filename or the SQL content."""
    hits: list[AntiPattern] = []
    base = os.path.basename(filename)
    for ap in ANTI_PATTERNS:
        if any(rx.search(base) for rx in ap.filename):
            hits.append(ap)
            continue
        if sql_text and any(rx.search(sql_text) for rx in ap.content):
            hits.append(ap)
    return hits


# ---------------------------------------------------------------------------
# Statistics
# ---------------------------------------------------------------------------

def wilson_lower_bound(successes: int, trials: int, z: float = 1.96) -> float:
    """Wilson score lower bound of a binomial proportion.

    Small-sample-safe alternative to raw p = k/n. A 1/1 outcome yields ~0.21,
    not 1.0, which prevents a single run from crowning a "100% offender".
    """
    if trials == 0:
        return 0.0
    p = successes / trials
    denom = 1 + z * z / trials
    center = p + z * z / (2 * trials)
    margin = z * math.sqrt(
        (p * (1 - p) + z * z / (4 * trials)) / trials
    )
    return max(0.0, (center - margin) / denom)


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------

@dataclass
class FileStats:
    filename: str
    total_runs: int = 0
    failed_runs: int = 0
    hygiene_failures: int = 0
    config_failures: int = 0
    example_run_urls: list[str] = field(default_factory=list)
    patterns: list[AntiPattern] = field(default_factory=list)

    @property
    def failure_rate(self) -> float:
        return self.failed_runs / self.total_runs if self.total_runs else 0.0

    @property
    def wilson(self) -> float:
        return wilson_lower_bound(self.failed_runs, self.total_runs)


def aggregate(runs: Iterable[Run], repo_path: Path | None) -> dict[str, FileStats]:
    stats: dict[str, FileStats] = defaultdict(lambda: FileStats(filename=""))
    for run in runs:
        for f in run.migration_files:
            fs = stats[f]
            fs.filename = f
            fs.total_runs += 1
            if run.overall_status in ("BLOCKED", "ERROR"):
                fs.failed_runs += 1
                if run.run_url and len(fs.example_run_urls) < 3:
                    fs.example_run_urls.append(run.run_url)
            if run.hygiene_status == "FAIL":
                fs.hygiene_failures += 1
            if run.config_status == "FAIL":
                fs.config_failures += 1

    # Classify each file once, after aggregation, so we don't reread SQL per-run.
    for fs in stats.values():
        sql_text = None
        if repo_path is not None:
            candidate = repo_path / fs.filename
            if candidate.is_file():
                try:
                    sql_text = candidate.read_text(errors="replace")
                except OSError:
                    sql_text = None
        fs.patterns = classify_file(fs.filename, sql_text)
    return stats


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def render_report(
    runs: list[Run],
    stats: dict[str, FileStats],
    top_n: int,
    min_runs: int,
    fmt: str,
) -> str:
    total_runs = len(runs)
    failed = sum(
        1 for r in runs if r.overall_status in ("BLOCKED", "ERROR")
    )
    hygiene_failed = sum(1 for r in runs if r.hygiene_status == "FAIL")
    config_failed = sum(1 for r in runs if r.config_status == "FAIL")

    eligible = [fs for fs in stats.values() if fs.total_runs >= min_runs]
    # Rank primarily by Wilson lower bound; break ties with raw failure count,
    # then total runs. This favors "consistently fails" over "failed once".
    eligible.sort(
        key=lambda fs: (fs.wilson, fs.failed_runs, fs.total_runs), reverse=True
    )
    top = [fs for fs in eligible if fs.failed_runs > 0][:top_n]

    lines: list[str] = []
    is_md = fmt == "md"
    h1 = "# " if is_md else ""
    h2 = "## " if is_md else "\n"
    b = "**" if is_md else ""

    lines.append(f"{h1}Migration Preflight Correlation Report")
    lines.append("")
    if total_runs == 0:
        lines.append("_No runs found in the tracker yet. Merge the workflow and let a few PRs run._")
        return "\n".join(lines)

    lines.append(f"{h2}Overall")
    lines.append("")
    lines.append(f"- Total runs analyzed: {b}{total_runs}{b}")
    lines.append(
        f"- Runs blocked: {b}{failed}{b} "
        f"({failed / total_runs:.0%})"
    )
    lines.append(
        f"- Config verifier failures: {b}{config_failed}{b} "
        f"({config_failed / total_runs:.0%})"
    )
    lines.append(
        f"- Session hygiene failures: {b}{hygiene_failed}{b} "
        f"({hygiene_failed / total_runs:.0%})"
    )
    lines.append(f"- Distinct migration files seen: {b}{len(stats)}{b}")
    if total_runs < 10:
        lines.append("")
        lines.append(
            "> _Sample size is small (<10 runs). Rankings use the Wilson 95% "
            "lower bound of the failure rate, so a single 1/1 failure does not "
            "get labeled 100%. Treat findings as provisional until you have "
            "at least ~20 runs across recurring files._"
        )
    lines.append("")

    lines.append(f"{h2}Top {min(top_n, len(top))} persistent offenders")
    lines.append("")
    if not top:
        lines.append("_No migration files have failed yet. Nothing to correlate._")
        return "\n".join(lines)

    for rank, fs in enumerate(top, 1):
        provisional = " _(provisional — <5 runs)_" if fs.total_runs < 5 else ""
        lines.append(
            f"{'### ' if is_md else ''}#{rank} `{fs.filename}`{provisional}"
        )
        lines.append("")
        lines.append(
            f"- Failure rate: {b}{fs.failed_runs}/{fs.total_runs}{b} "
            f"({fs.failure_rate:.0%}) — Wilson 95% lower bound: {fs.wilson:.0%}"
        )
        lines.append(
            f"- Hygiene failures: {fs.hygiene_failures}, "
            f"Config failures: {fs.config_failures}"
        )
        if fs.example_run_urls:
            urls = ", ".join(f"[run]({u})" if is_md else u for u in fs.example_run_urls)
            lines.append(f"- Example failed runs: {urls}")

        if fs.patterns:
            lines.append("")
            lines.append(f"{b}DDL anti-patterns detected:{b}")
            lines.append("")
            for ap in fs.patterns:
                lines.append(
                    f"- {b}{ap.name}{b} — most likely trips "
                    f"{ap.trigger} check(s)."
                )
                lines.append(f"  - {ap.why}")
        else:
            lines.append("")
            lines.append(
                "_No known anti-patterns detected by filename. Re-run with "
                "`--repo-path` to enable content-based detection._"
            )
        lines.append("")

    lines.append(f"{h2}Recommendations")
    lines.append("")
    recs = build_recommendations(top)
    if not recs:
        lines.append("_No pattern-specific recommendations — see per-file notes above._")
    else:
        for r in recs:
            lines.append(f"- {r}")
    lines.append("")
    return "\n".join(lines)


def build_recommendations(top: list[FileStats]) -> list[str]:
    """Turn the detected patterns into concrete, deduplicated remediation steps."""
    seen: set[str] = set()
    recs: list[str] = []
    remediation = {
        "Blocking CREATE INDEX (missing CONCURRENTLY)":
            "Use `CREATE INDEX CONCURRENTLY` and run it outside the migration "
            "transaction. Add a lint check to reject `CREATE INDEX` without "
            "`CONCURRENTLY` in `db/migrations/**`.",
        "ALTER TABLE ... ADD COLUMN NOT NULL DEFAULT (non-constant)":
            "Split into 3 migrations: (1) `ADD COLUMN` nullable with no default, "
            "(2) backfill in batches, (3) `SET NOT NULL` and `SET DEFAULT`. "
            "PG 11+ handles constant defaults without a rewrite — verify the "
            "default is a literal, not `now()` / `gen_random_uuid()` / etc.",
        "ALTER TABLE ... ADD FOREIGN KEY without NOT VALID":
            "Add the FK as `NOT VALID`, then run `ALTER TABLE ... VALIDATE "
            "CONSTRAINT` in a separate migration. Validate only takes a SHARE "
            "UPDATE EXCLUSIVE lock and can run online.",
        "Single-statement bulk UPDATE / backfill":
            "Batch backfills into chunks of ~10k rows per commit, using a "
            "keyset loop with `LIMIT` and explicit commit boundaries. "
            "Session `synchronous_commit = off` for the loop; never global.",
        "ALTER TYPE ... ADD VALUE inside a transaction":
            "Move `ALTER TYPE ... ADD VALUE` to its own migration with no "
            "surrounding DDL. Ensure PG version ≥ 12 if you need transactional "
            "semantics.",
        "ALTER COLUMN TYPE (implicit rewrite)":
            "Prefer add-new-column + backfill + rename over `ALTER COLUMN TYPE` "
            "on large tables. Binary-compatible casts (e.g. `varchar` → `text`) "
            "are safe; anything else forces a rewrite.",
        "DROP COLUMN on hot table":
            "Deploy code that stops referencing the column before the DROP. "
            "Consider marking the column as `NOT NULL DEFAULT` unused-sentinel "
            "and dropping in a follow-up release window.",
        "Explicit LOCK TABLE":
            "Remove explicit `LOCK TABLE`. If serialization is required, use "
            "advisory locks (`pg_advisory_xact_lock`) which don't block DML.",
        "VACUUM FULL / CLUSTER":
            "Move `VACUUM FULL` / `CLUSTER` out of migration files entirely. "
            "Schedule as a maintenance window task; prefer `pg_repack` for "
            "online rewrites.",
        "REINDEX (non-concurrent)":
            "Use `REINDEX INDEX CONCURRENTLY` / `REINDEX TABLE CONCURRENTLY` "
            "(PG 12+).",
    }
    for fs in top:
        for ap in fs.patterns:
            if ap.name not in seen and ap.name in remediation:
                recs.append(remediation[ap.name])
                seen.add(ap.name)
    return recs


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--repo-path",
        type=Path,
        default=None,
        help="Path to the repo root; enables SQL content-based pattern detection.",
    )
    p.add_argument("--min-runs", type=int, default=1)
    p.add_argument("--top", type=int, default=3)
    p.add_argument("--since", default=None, help="YYYY-MM-DD lower bound on Run Time.")
    p.add_argument("--format", choices=["text", "md"], default="text")
    p.add_argument(
        "--dry-run-fixture",
        default=None,
        help="Path to a JSON file with a canned Notion query response, used "
        "for offline testing. When set, NOTION_TOKEN is not required.",
    )
    args = p.parse_args()

    if args.dry_run_fixture:
        with open(args.dry_run_fixture) as f:
            fixture = json.load(f)
        raw_rows = fixture.get("results", [])
    else:
        token = os.environ.get("NOTION_TOKEN", "").strip()
        db_id = os.environ.get("NOTION_DATABASE_ID", "").strip()
        if not token or not db_id:
            print(
                "error: set NOTION_TOKEN and NOTION_DATABASE_ID, or pass "
                "--dry-run-fixture for offline testing.",
                file=sys.stderr,
            )
            return 2
        since_iso = None
        if args.since:
            try:
                since_iso = datetime.strptime(args.since, "%Y-%m-%d").date().isoformat()
            except ValueError:
                print(f"error: --since must be YYYY-MM-DD, got {args.since!r}", file=sys.stderr)
                return 2
        raw_rows = fetch_all_rows(token, db_id, since_iso)

    runs = [parse_row(r) for r in raw_rows]
    stats = aggregate(runs, args.repo_path)
    print(render_report(runs, stats, args.top, args.min_runs, args.format))
    return 0


if __name__ == "__main__":
    sys.exit(main())
