#!/usr/bin/env python3
"""catalog-schema check: validate catalog.json against the Odisena Console data model.

Deterministic, stdlib-only, no network. Validates the actual shape produced by
this repo (categories / sessions / runbooks / artifacts / stats) and the
cross-references between them. Exits non-zero on any violation.
"""
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CATALOG = ROOT / "catalog.json"
HEX = re.compile(r"^#[0-9a-fA-F]{6}$")


def main() -> int:
    errors: list[str] = []
    try:
        data = json.loads(CATALOG.read_text(encoding="utf-8"))
    except FileNotFoundError:
        print(f"FAIL: {CATALOG} not found")
        return 1
    except json.JSONDecodeError as exc:
        print(f"FAIL: catalog.json is not valid JSON: {exc}")
        return 1

    if not isinstance(data, dict):
        print("FAIL: catalog.json top level must be an object")
        return 1

    for key in ("categories", "sessions", "runbooks", "artifacts", "stats"):
        if key not in data:
            errors.append(f"missing top-level key: {key}")
    if errors:
        for e in errors:
            print(f"FAIL: {e}")
        return 1

    categories = data["categories"]
    if not isinstance(categories, dict) or not categories:
        errors.append("categories must be a non-empty object")
    cat_keys = set(categories) if isinstance(categories, dict) else set()
    if isinstance(categories, dict):
        for ck, cv in categories.items():
            if not isinstance(cv, dict):
                errors.append(f"category {ck!r} must be an object")
                continue
            if not isinstance(cv.get("name"), str) or not cv["name"].strip():
                errors.append(f"category {ck!r}: name must be a non-empty string")
            if not (isinstance(cv.get("color"), str) and HEX.match(cv["color"])):
                errors.append(f"category {ck!r}: color must be a #rrggbb hex string")
            if not isinstance(cv.get("count"), int) or cv["count"] < 0:
                errors.append(f"category {ck!r}: count must be a non-negative int")

    sessions = data["sessions"]
    session_ids: set[str] = set()
    if not isinstance(sessions, list):
        errors.append("sessions must be an array")
    else:
        for i, s in enumerate(sessions):
            where = f"sessions[{i}]"
            if not isinstance(s, dict):
                errors.append(f"{where} must be an object")
                continue
            sid = s.get("id")
            if not isinstance(sid, str) or not sid:
                errors.append(f"{where}: id must be a non-empty string")
            else:
                if sid in session_ids:
                    errors.append(f"{where}: duplicate session id {sid!r}")
                session_ids.add(sid)
            if not isinstance(s.get("title"), str) or not s["title"].strip():
                errors.append(f"{where}: title must be a non-empty string")
            if s.get("category") not in cat_keys:
                errors.append(f"{where}: category {s.get('category')!r} not in categories")
            if not isinstance(s.get("files"), list) or not all(
                isinstance(f, str) for f in s.get("files", [])
            ):
                errors.append(f"{where}: files must be an array of strings")

    for coll, expected_type, prefix in (
        ("runbooks", "runbook", "runbooks/"),
        ("artifacts", "artifact", "artifacts/"),
    ):
        items = data[coll]
        if not isinstance(items, list):
            errors.append(f"{coll} must be an array")
            continue
        for i, it in enumerate(items):
            where = f"{coll}[{i}]"
            if not isinstance(it, dict):
                errors.append(f"{where} must be an object")
                continue
            for field in ("session", "session_title", "category", "name",
                          "display_name", "ext", "type", "path"):
                if not isinstance(it.get(field), str) or not it.get(field):
                    errors.append(f"{where}: {field} must be a non-empty string")
            if it.get("type") != expected_type:
                errors.append(f"{where}: type must be {expected_type!r}, got {it.get('type')!r}")
            if isinstance(it.get("path"), str) and not it["path"].startswith(prefix):
                errors.append(f"{where}: path {it['path']!r} must start with {prefix!r}")
            if not isinstance(it.get("size"), int) or it.get("size", -1) < 0:
                errors.append(f"{where}: size must be a non-negative int")
            if isinstance(it.get("session"), str) and it["session"] not in session_ids:
                errors.append(f"{where}: session {it['session']!r} has no matching sessions[] entry")

    stats = data["stats"]
    if not isinstance(stats, dict):
        errors.append("stats must be an object")
    else:
        expected = {
            "total_sessions": len(sessions) if isinstance(sessions, list) else None,
            "total_runbooks": len(data["runbooks"]) if isinstance(data["runbooks"], list) else None,
            "total_artifacts": len(data["artifacts"]) if isinstance(data["artifacts"], list) else None,
        }
        for field, exp in expected.items():
            val = stats.get(field)
            if not isinstance(val, int):
                errors.append(f"stats.{field} must be an int")
            elif exp is not None and val != exp:
                errors.append(f"stats.{field}={val} but actual count is {exp}")
        rb = expected["total_runbooks"] or 0
        ar = expected["total_artifacts"] or 0
        if isinstance(stats.get("total_files"), int):
            if stats["total_files"] != rb + ar:
                errors.append(f"stats.total_files={stats['total_files']} but runbooks+artifacts={rb + ar}")
        else:
            errors.append("stats.total_files must be an int")

    if errors:
        print(f"catalog-schema: FAIL ({len(errors)} issue(s))")
        for e in errors:
            print(f"  - {e}")
        return 1

    print(
        "catalog-schema: OK "
        f"({len(categories)} categories, {len(sessions)} sessions, "
        f"{len(data['runbooks'])} runbooks, {len(data['artifacts'])} artifacts)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
