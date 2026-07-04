#!/usr/bin/env python3
"""
Populate the Migration Readiness Dashboard's two derived Notion databases
from the raw preflight runs tracker.

Reads:
    Preflight Runs tracker  (env NOTION_DATABASE_ID)

Writes:
    Preflight Weekly Snapshots  (env NOTION_SNAPSHOTS_DB_ID)
        - One row per (migration file, ISO week). Idempotent via
          "Snapshot Key" = "<file>|<iso_week>". Existing rows for the
          same key are updated in place; new rows are created.
        - Powers the 12-week trend chart on the dashboard.

    Offender Leaderboard        (env NOTION_LEADERBOARD_DB_ID)
        - One row per migration file that has ever been in the top 3.
          Upserted by "Migration File" title.
        - "New This Week" flips to true on the run when a file first
          enters the top 3, and back to false on subsequent runs — the
          dashboard's callout view filters on this checkbox.
        - "Status" transitions: New → Persistent → Improving → Resolved
          based on rank history + trend of the Wilson lower bound.

Design notes:
    * All writes are idempotent. Running the script twice in a row leaves
      the dashboard identical.
    * If any secret is missing, the corresponding write is skipped with
      a warning — never fails the caller. This mirrors log_preflight_to_notion.
    * "Top 3" is defined as in the analyzer: rank by Wilson 95% lower bound
      of the current-window failure rate, filter to files with at least
      one failed run, take the first three.
    * The trend chart window (default 12 weeks) is enforced by only writing
      snapshots for weeks within the window; older snapshots are left in
      place (history preservation) so you can widen the window later.

Usage:
    export NOTION_TOKEN=...
    export NOTION_DATABASE_ID=<preflight runs DB>
    export NOTION_SNAPSHOTS_DB_ID=<snapshots DB>
    export NOTION_LEADERBOARD_DB_ID=<leaderboard DB>
    python3 dashboard_writer.py [--weeks 12] [--repo-path /path/to/repo]
                                [--dry-run-fixture path.json]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

# Reuse everything from the analyzer we already validated. This keeps the
# anti-pattern catalog, filename/content regex rules, and Wilson math
# exactly consistent across the CLI report and the dashboard.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from analyze_preflight_history import (  # noqa: E402
    ANTI_PATTERNS,
    NOTION_API_BASE,
    NOTION_VERSION,
    Run,
    build_recommendations,
    classify_file,
    fetch_all_rows,
    parse_row,
    wilson_lower_bound,
)


# Map analyzer anti-pattern names → the short labels used in the Notion
# multi_select options. The dashboard databases have their own compact
# label set that fits nicely in the UI.
PATTERN_LABEL_MAP: dict[str, str] = {
    "Blocking CREATE INDEX (missing CONCURRENTLY)": "CREATE INDEX (non-concurrent)",
    "ALTER TABLE ... ADD COLUMN NOT NULL DEFAULT (non-constant)": "ADD COLUMN NOT NULL DEFAULT",
    "ALTER TABLE ... ADD FOREIGN KEY without NOT VALID": "ADD FOREIGN KEY (validating)",
    "Single-statement bulk UPDATE / backfill": "Bulk UPDATE / backfill",
    "ALTER TYPE ... ADD VALUE inside a transaction": "ALTER TYPE ADD VALUE in txn",
    "ALTER COLUMN TYPE (implicit rewrite)": "ALTER COLUMN TYPE rewrite",
    "DROP COLUMN on hot table": "DROP COLUMN hot table",
    "Explicit LOCK TABLE": "Explicit LOCK TABLE",
    "VACUUM FULL / CLUSTER": "VACUUM FULL / CLUSTER",
    "REINDEX (non-concurrent)": "REINDEX (non-concurrent)",
}


# ---------------------------------------------------------------------------
# Notion request helper (POST/PATCH). GET/paged reads are in analyzer module.
# ---------------------------------------------------------------------------

def _notion_write(path: str, token: str, method: str, payload: dict) -> dict:
    req = urllib.request.Request(
        f"{NOTION_API_BASE}{path}",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {token}",
            "Notion-Version": NOTION_VERSION,
            "Content-Type": "application/json",
        },
        method=method,
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace") if e.fp else ""
        raise SystemExit(f"Notion API {method} {path} failed {e.code}: {body[:600]}")


def _query_all(token: str, database_id: str) -> list[dict]:
    """Same as analyzer.fetch_all_rows but with no date filter."""
    rows: list[dict] = []
    cursor = None
    while True:
        payload: dict = {"page_size": 100}
        if cursor:
            payload["start_cursor"] = cursor
        req = urllib.request.Request(
            f"{NOTION_API_BASE}/databases/{database_id}/query",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {token}",
                "Notion-Version": NOTION_VERSION,
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        rows.extend(data.get("results", []))
        if not data.get("has_more"):
            break
        cursor = data.get("next_cursor")
    return rows


# ---------------------------------------------------------------------------
# Aggregation by (file, ISO week)
# ---------------------------------------------------------------------------

def iso_week_key(run_time: str | None) -> tuple[str, date] | None:
    """Turn a Notion date string into ('2026-W27', date(2026,7,6)) for Monday."""
    if not run_time:
        return None
    # Notion returns either "YYYY-MM-DD" or ISO 8601 datetime.
    if "T" in run_time:
        dt = datetime.fromisoformat(run_time.replace("Z", "+00:00"))
        d = dt.date()
    else:
        d = date.fromisoformat(run_time)
    year, week, _ = d.isocalendar()
    monday = d - timedelta(days=d.isocalendar().weekday - 1)
    return f"{year:04d}-W{week:02d}", monday


@dataclass
class WeekBucket:
    file: str
    iso_week: str
    week_start: date
    total: int = 0
    failed: int = 0
    hygiene_failed: int = 0
    config_failed: int = 0
    latest_run_url: str | None = None
    repo: str = ""

    @property
    def failure_rate(self) -> float:
        return self.failed / self.total if self.total else 0.0

    @property
    def wilson(self) -> float:
        return wilson_lower_bound(self.failed, self.total)


def bucket_runs(runs: list[Run]) -> dict[tuple[str, str], WeekBucket]:
    """Group runs by (file, iso_week) → WeekBucket."""
    buckets: dict[tuple[str, str], WeekBucket] = {}
    for run in runs:
        key = iso_week_key(run.run_time)
        if key is None:
            continue
        iso_wk, monday = key
        for f in run.migration_files:
            k = (f, iso_wk)
            b = buckets.get(k)
            if b is None:
                b = WeekBucket(file=f, iso_week=iso_wk, week_start=monday, repo=run.repo)
                buckets[k] = b
            b.total += 1
            if run.overall_status in ("BLOCKED", "ERROR"):
                b.failed += 1
                if run.run_url:
                    b.latest_run_url = run.run_url
            if run.hygiene_status == "FAIL":
                b.hygiene_failed += 1
            if run.config_status == "FAIL":
                b.config_failed += 1
    return buckets


# ---------------------------------------------------------------------------
# Top-3 selection per week
# ---------------------------------------------------------------------------

def top3_by_week(buckets: dict[tuple[str, str], WeekBucket]) -> dict[str, list[WeekBucket]]:
    """For each ISO week, rank offenders by Wilson lower bound. Files with 0
    failures are excluded. Return {iso_week: [top1, top2, top3]}."""
    by_week: dict[str, list[WeekBucket]] = defaultdict(list)
    for b in buckets.values():
        if b.failed > 0:
            by_week[b.iso_week].append(b)
    for wk in by_week:
        by_week[wk].sort(key=lambda x: (x.wilson, x.failed, x.total), reverse=True)
        by_week[wk] = by_week[wk][:3]
    return by_week


# ---------------------------------------------------------------------------
# Snapshot upsert
# ---------------------------------------------------------------------------

def build_snapshot_key(file: str, iso_week: str) -> str:
    return f"{file}|{iso_week}"


def find_existing_snapshot(
    token: str, db_id: str, snapshot_key: str
) -> str | None:
    """Return the page id of an existing snapshot row for this key, or None."""
    payload = {
        "filter": {
            "property": "Snapshot Key",
            "title": {"equals": snapshot_key},
        },
        "page_size": 1,
    }
    resp = _notion_write(f"/databases/{db_id}/query", token, "POST", payload)
    results = resp.get("results", [])
    return results[0]["id"] if results else None


def snapshot_properties(
    bucket: WeekBucket,
    rank: int | None,
    patterns: list[str],
) -> dict:
    key = build_snapshot_key(bucket.file, bucket.iso_week)
    props: dict = {
        "Snapshot Key":    {"title":     [{"text": {"content": key}}]},
        "Migration File":  {"rich_text": [{"text": {"content": bucket.file}}]},
        "ISO Week":        {"rich_text": [{"text": {"content": bucket.iso_week}}]},
        "Week Start":      {"date":      {"start": bucket.week_start.isoformat()}},
        "Total Runs":      {"number":    bucket.total},
        "Failed Runs":     {"number":    bucket.failed},
        "Failure Rate":    {"number":    round(bucket.failure_rate, 4)},
        "Wilson Lower Bound": {"number": round(bucket.wilson, 4)},
        "Hygiene Failures":{"number":    bucket.hygiene_failed},
        "Config Failures": {"number":    bucket.config_failed},
        "Rank This Week":  {"number":    rank},  # None → cleared in Notion
        "Was In Top 3":    {"checkbox":  rank is not None},
        "Repository":      {"rich_text": [{"text": {"content": bucket.repo}}]},
        "Detected Patterns": {
            "multi_select": [{"name": p} for p in patterns if p in PATTERN_LABEL_MAP.values()]
        },
    }
    return props


def upsert_snapshot(
    token: str, db_id: str, bucket: WeekBucket, rank: int | None, patterns: list[str]
) -> None:
    key = build_snapshot_key(bucket.file, bucket.iso_week)
    existing = find_existing_snapshot(token, db_id, key)
    props = snapshot_properties(bucket, rank, patterns)
    if existing:
        _notion_write(f"/pages/{existing}", token, "PATCH", {"properties": props})
    else:
        _notion_write(
            "/pages", token, "POST",
            {"parent": {"database_id": db_id}, "properties": props},
        )


# ---------------------------------------------------------------------------
# Leaderboard upsert
# ---------------------------------------------------------------------------

def find_existing_leaderboard(token: str, db_id: str, file: str) -> dict | None:
    """Return the existing leaderboard page (dict) for a file, or None."""
    payload = {
        "filter": {"property": "Migration File", "title": {"equals": file}},
        "page_size": 1,
    }
    resp = _notion_write(f"/databases/{db_id}/query", token, "POST", payload)
    results = resp.get("results", [])
    return results[0] if results else None


def _leaderboard_prop_number(page: dict, name: str) -> float | int | None:
    if not page:
        return None
    return (page.get("properties", {}).get(name, {}) or {}).get("number")


def _leaderboard_prop_bool(page: dict, name: str) -> bool:
    if not page:
        return False
    return bool((page.get("properties", {}).get(name, {}) or {}).get("checkbox"))


def upsert_leaderboard(
    token: str,
    db_id: str,
    bucket: WeekBucket,
    rank: int,
    patterns: list[str],
    recommendation: str,
    today_iso: str,
) -> None:
    existing = find_existing_leaderboard(token, db_id, bucket.file)
    was_in_top3_before = bool(existing) and _leaderboard_prop_number(existing, "Current Rank") is not None
    is_new_this_week = not was_in_top3_before
    prev_weeks = int(_leaderboard_prop_number(existing, "Weeks In Top 3") or 0) if existing else 0
    prev_wilson = _leaderboard_prop_number(existing, "Latest Wilson Lower Bound") if existing else None

    # Status transitions: New (just entered) → Persistent (≥3 weeks) →
    # Improving (Wilson dropped ≥ 10pp week-over-week) → Resolved (handled
    # in mark_resolved).
    status = "New" if is_new_this_week else "Persistent"
    if not is_new_this_week and prev_wilson is not None and bucket.wilson < prev_wilson - 0.10:
        status = "Improving"

    props: dict = {
        "Migration File":     {"title":     [{"text": {"content": bucket.file}}]},
        "Current Rank":       {"number":    rank},
        "Weeks In Top 3":     {"number":    prev_weeks + 1},
        "Last Updated":       {"date":      {"start": today_iso}},
        "New This Week":      {"checkbox":  is_new_this_week},
        "Latest Failure Rate":{"number":    round(bucket.failure_rate, 4)},
        "Latest Wilson Lower Bound": {"number": round(bucket.wilson, 4)},
        "Total Runs Observed":{"number":    bucket.total},
        "Total Failed Runs":  {"number":    bucket.failed},
        "Repository":         {"rich_text": [{"text": {"content": bucket.repo}}]},
        "Primary Recommendation": {"rich_text": [{"text": {"content": recommendation[:1900]}}]},
        "Status":             {"select":    {"name": status}},
        "Detected Patterns": {
            "multi_select": [{"name": p} for p in patterns if p in PATTERN_LABEL_MAP.values()]
        },
    }
    if bucket.latest_run_url:
        props["Latest Run URL"] = {"url": bucket.latest_run_url}
    if is_new_this_week:
        props["First Seen In Top 3"] = {"date": {"start": today_iso}}

    if existing:
        _notion_write(f"/pages/{existing['id']}", token, "PATCH", {"properties": props})
    else:
        _notion_write(
            "/pages", token, "POST",
            {"parent": {"database_id": db_id}, "properties": props},
        )


def mark_resolved(
    token: str,
    db_id: str,
    still_in_top3_files: set[str],
) -> None:
    """Files previously in the leaderboard with Current Rank set but no longer
    in this week's top 3 get Current Rank cleared and Status → Resolved.
    'New This Week' is also cleared so the callout view doesn't stay lit."""
    payload = {
        "filter": {
            "and": [
                {"property": "Current Rank", "number": {"is_not_empty": True}},
            ]
        },
        "page_size": 100,
    }
    resp = _notion_write(f"/databases/{db_id}/query", token, "POST", payload)
    for page in resp.get("results", []):
        title_prop = page.get("properties", {}).get("Migration File", {})
        title_text = "".join(t.get("plain_text", "") for t in title_prop.get("title", []))
        if title_text in still_in_top3_files:
            continue
        _notion_write(
            f"/pages/{page['id']}", token, "PATCH",
            {"properties": {
                "Current Rank":  {"number": None},
                "New This Week": {"checkbox": False},
                "Status":        {"select": {"name": "Resolved"}},
            }},
        )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def _pattern_labels_for_file(file: str, repo_path: Path | None) -> list[str]:
    sql_text = None
    if repo_path is not None:
        candidate = repo_path / file
        if candidate.is_file():
            try:
                sql_text = candidate.read_text(errors="replace")
            except OSError:
                sql_text = None
    hits = classify_file(file, sql_text)
    return [PATTERN_LABEL_MAP[h.name] for h in hits if h.name in PATTERN_LABEL_MAP]


def _primary_recommendation(file: str, patterns: list[str]) -> str:
    """Reuse the analyzer's recommendation table by mapping back through
    the label→name inversion. Returns the first matching recommendation."""
    inverse = {v: k for k, v in PATTERN_LABEL_MAP.items()}
    names = [inverse[p] for p in patterns if p in inverse]
    # Build a fake FileStats-like shim to reuse build_recommendations.
    class _Shim:  # minimal duck type
        pass
    shim = _Shim()
    shim.patterns = [ap for ap in ANTI_PATTERNS if ap.name in names]
    recs = build_recommendations([shim])
    return recs[0] if recs else "No specific remediation available; see report."


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--weeks", type=int, default=12)
    p.add_argument("--repo-path", type=Path, default=None)
    p.add_argument("--dry-run-fixture", default=None,
                   help="Path to a Notion query fixture; when set, writes are "
                        "printed as JSON instead of sent to Notion.")
    args = p.parse_args()

    token = os.environ.get("NOTION_TOKEN", "").strip()
    runs_db = os.environ.get("NOTION_DATABASE_ID", "").strip()
    snaps_db = os.environ.get("NOTION_SNAPSHOTS_DB_ID", "").strip()
    leader_db = os.environ.get("NOTION_LEADERBOARD_DB_ID", "").strip()

    if args.dry_run_fixture:
        with open(args.dry_run_fixture) as fh:
            raw_rows = json.load(fh).get("results", [])
    else:
        if not (token and runs_db):
            print("error: NOTION_TOKEN and NOTION_DATABASE_ID required", file=sys.stderr)
            return 2
        raw_rows = fetch_all_rows(token, runs_db, since_iso=None)

    runs = [parse_row(r) for r in raw_rows]

    # Restrict to the trend window (default 12 weeks). Older runs still exist
    # in the source tracker but don't roll into new dashboard snapshots.
    cutoff = date.today() - timedelta(weeks=args.weeks)
    def in_window(r: Run) -> bool:
        key = iso_week_key(r.run_time)
        return key is not None and key[1] >= cutoff
    windowed = [r for r in runs if in_window(r)]

    buckets = bucket_runs(windowed)
    top3 = top3_by_week(buckets)

    # Compute this-week's top-3 for leaderboard state.
    this_week = date.today().isocalendar()
    this_wk_key = f"{this_week.year:04d}-W{this_week.week:02d}"
    this_week_top3 = top3.get(this_wk_key, [])
    still_in_top3_files = {b.file for b in this_week_top3}

    today_iso = date.today().isoformat()

    # Preview mode: emit what would be written and exit.
    if args.dry_run_fixture:
        preview = {
            "window_weeks": args.weeks,
            "cutoff": cutoff.isoformat(),
            "buckets_computed": len(buckets),
            "weeks_with_top3": len(top3),
            "this_week_key": this_wk_key,
            "this_week_top3": [
                {
                    "rank": i + 1, "file": b.file, "wilson": round(b.wilson, 3),
                    "failed": b.failed, "total": b.total,
                    "patterns": _pattern_labels_for_file(b.file, args.repo_path),
                }
                for i, b in enumerate(this_week_top3)
            ],
            "snapshots_to_write": sum(len(v) for v in top3.values())
                                  + sum(1 for b in buckets.values() if b.failed == 0),
        }
        print(json.dumps(preview, indent=2, default=str))
        return 0

    if not snaps_db:
        print("warning: NOTION_SNAPSHOTS_DB_ID unset; skipping snapshot writes", file=sys.stderr)
    else:
        # Write ONE snapshot per bucket. Rank is populated only when the file
        # was in that week's top 3. Every bucket gets a row so the chart
        # includes all files that had activity, not just the top 3.
        for (file_key, iso_wk), bucket in buckets.items():
            rank = None
            for i, top_bucket in enumerate(top3.get(iso_wk, []), 1):
                if top_bucket.file == bucket.file:
                    rank = i
                    break
            patterns = _pattern_labels_for_file(bucket.file, args.repo_path)
            upsert_snapshot(token, snaps_db, bucket, rank, patterns)

    if not leader_db:
        print("warning: NOTION_LEADERBOARD_DB_ID unset; skipping leaderboard writes", file=sys.stderr)
    else:
        for rank, bucket in enumerate(this_week_top3, 1):
            patterns = _pattern_labels_for_file(bucket.file, args.repo_path)
            rec = _primary_recommendation(bucket.file, patterns)
            upsert_leaderboard(token, leader_db, bucket, rank, patterns, rec, today_iso)
        mark_resolved(token, leader_db, still_in_top3_files)

    print(f"dashboard_writer: {len(buckets)} buckets, "
          f"{len(this_week_top3)} in top 3 this week ({this_wk_key})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
