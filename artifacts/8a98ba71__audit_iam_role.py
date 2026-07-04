#!/usr/bin/env python3
"""
IAM Least-Privilege Auditor
===========================

Analyzes 90 days of CloudTrail activity for a specific IAM role, diffs it
against the role's current allow surface, generates a refactored least-
privilege policy, validates it via IAM Access Analyzer, and prints a summary.

Usage
-----
    python audit_iam_role.py --role-name MyReadOnlyRole \\
        --profile prod \\
        --out-dir ./audit-output

    # Optional flags
    --lookback-days 90
    --regions us-east-1,us-west-2
    --policy-type IDENTITY_POLICY      # or RESOURCE_POLICY / SERVICE_CONTROL_POLICY
    --skip-validation                  # for offline dry-runs
    --events-file ./trail.jsonl        # feed pre-collected events instead of API

Required IAM permissions on the *auditor* principal
---------------------------------------------------
    cloudtrail:LookupEvents
    iam:GetRole, iam:ListRolePolicies, iam:GetRolePolicy,
    iam:ListAttachedRolePolicies, iam:GetPolicy, iam:GetPolicyVersion
    access-analyzer:ValidatePolicy, access-analyzer:CheckNoNewAccess
    ec2:DescribeRegions           (only when --regions is omitted)
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path

import boto3

from iam_auditor.action_matcher import match_statements, summarize_usage
from iam_auditor.cloudtrail_collector import CloudTrailCollector, TrailEvent
from iam_auditor.policy_builder import build_refactored_policy
from iam_auditor.policy_loader import load_role_policies
from iam_auditor.reporter import build_summary
from iam_auditor.validator import ValidationReport, validate_policy


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="IAM least-privilege auditor")
    p.add_argument("--role-name", required=True, help="IAM role to audit")
    p.add_argument("--profile", help="AWS CLI profile to use")
    p.add_argument("--region", default="us-east-1",
                   help="Region for Access Analyzer calls (default: us-east-1)")
    p.add_argument("--regions", help="Comma-separated CloudTrail regions "
                                     "(default: auto-discover)")
    p.add_argument("--lookback-days", type=int, default=90)
    p.add_argument("--policy-type", default="IDENTITY_POLICY",
                   choices=["IDENTITY_POLICY", "RESOURCE_POLICY",
                            "SERVICE_CONTROL_POLICY"])
    p.add_argument("--out-dir", default="./audit-output")
    p.add_argument("--skip-validation", action="store_true")
    p.add_argument("--events-file",
                   help="Load CloudTrail events from a JSONL file instead of API")
    p.add_argument("-v", "--verbose", action="store_true")
    return p.parse_args()


def _session(profile: str | None) -> boto3.session.Session:
    if profile:
        return boto3.session.Session(profile_name=profile)
    return boto3.session.Session()


def _load_events_file(path: str) -> list[TrailEvent]:
    from datetime import datetime
    events: list[TrailEvent] = []
    with open(path, "r") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            et = rec.get("eventTime")
            if isinstance(et, str):
                try:
                    et = datetime.fromisoformat(et.replace("Z", "+00:00"))
                except ValueError:
                    et = None
            events.append(TrailEvent(
                event_time=et,
                event_source=rec.get("eventSource", ""),
                event_name=rec.get("eventName", ""),
                aws_region=rec.get("awsRegion", ""),
                resources=rec.get("resources") or [],
                request_parameters=rec.get("requestParameters") or {},
                error_code=rec.get("errorCode"),
                read_only=rec.get("readOnly"),
            ))
    return events


def main() -> int:
    args = parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    log = logging.getLogger("audit_iam_role")

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    session = _session(args.profile)

    # 1. Load current policy
    log.info("Loading current policies for role=%s", args.role_name)
    loaded = load_role_policies(args.role_name, session=session)
    (out_dir / "current_policy.json").write_text(
        json.dumps({
            "inline": loaded.inline_policies,
            "attached": loaded.attached_policies,
        }, indent=2)
    )

    # 2. Collect CloudTrail events
    if args.events_file:
        log.info("Loading events from %s", args.events_file)
        events = _load_events_file(args.events_file)
    else:
        regions = args.regions.split(",") if args.regions else None
        collector = CloudTrailCollector(
            role_name=args.role_name,
            session=session,
            regions=regions,
            lookback_days=args.lookback_days,
        )
        events = list(collector.iter_events())
    log.info("Collected %d CloudTrail events", len(events))

    # 3. Summarize usage and match against current statements
    usage = summarize_usage(events)
    statement_usages = match_statements(loaded, usage)

    # 4. Build refactored policy
    refactored = build_refactored_policy(statement_usages, usage)
    (out_dir / "refactored_policy.json").write_text(json.dumps(refactored, indent=2))
    log.info("Wrote refactored policy to %s", out_dir / "refactored_policy.json")

    # 5. Validate with Access Analyzer
    if args.skip_validation:
        validation = ValidationReport()
    else:
        # Build a "baseline" from the union of all Allow statements for
        # CheckNoNewAccess. We compose a single combined document.
        baseline = {
            "Version": "2012-10-17",
            "Statement": [
                stmt for _kind, _origin, stmt in loaded.iter_statements()
            ],
        }
        validation = validate_policy(
            policy_document=refactored,
            baseline_document=baseline,
            policy_type=args.policy_type,
            session=session,
            region=args.region,
        )
    (out_dir / "validation_findings.json").write_text(
        json.dumps({
            "check_no_new_access": validation.check_no_new_access,
            "findings": [f.__dict__ for f in validation.findings],
        }, indent=2)
    )

    # 6. Summary report
    summary = build_summary(
        role_name=args.role_name,
        lookback_days=args.lookback_days,
        events=events,
        usage=usage,
        statement_usages=statement_usages,
        refactored_policy=refactored,
        validation=validation,
    )
    (out_dir / "summary.json").write_text(summary.to_json())
    text = summary.to_text()
    (out_dir / "summary.txt").write_text(text)
    print(text)

    # Exit non-zero if Access Analyzer surfaced anything critical so this can
    # gate a CI pipeline.
    return 1 if validation.has_errors else 0


if __name__ == "__main__":
    sys.exit(main())
