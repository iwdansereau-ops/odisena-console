# IAM Least-Privilege Auditor

Analyze **90 days of CloudTrail activity** for a specific IAM role, diff observed
API calls against the role's current policy surface, and generate a refactored
**least-privilege** policy — validated by AWS IAM Access Analyzer.

## What it does

1. **Loads the current policy** — inline + all attached managed policies for the target role.
2. **Collects CloudTrail activity** — `LookupEvents` across all enabled regions, filtered by the role's `Username`, for the last N days (default 90).
3. **Maps used actions → resources** — groups every observed call into `service:Action` buckets and records the actual ARNs touched.
4. **Diffs against the current policy** — expands wildcards (`s3:Get*`, `arn:aws:s3:::*`) and identifies:
   - Actions **used** (retain)
   - Actions **granted but never called** (remove)
   - Statements with **zero observed usage** (flag)
5. **Builds a refactored policy** — one consolidated Allow per (service, resource-set), preserving every Deny statement verbatim.
6. **Validates with Access Analyzer** — calls `ValidatePolicy` for grammar/security findings and `CheckNoNewAccess` to prove the refactored policy grants no new permissions vs. the original.
7. **Emits a summary report** — human-readable text + machine-readable JSON.

## Install

```bash
pip install -r requirements.txt
```

## Usage

```bash
python audit_iam_role.py \
    --role-name MyReadOnlyRole \
    --profile prod \
    --lookback-days 90 \
    --out-dir ./audit-output
```

Optional flags:

| Flag | Description |
|---|---|
| `--regions us-east-1,us-west-2` | Restrict CloudTrail lookup to specific regions (default: auto-discover) |
| `--policy-type` | `IDENTITY_POLICY` (default), `RESOURCE_POLICY`, or `SERVICE_CONTROL_POLICY` |
| `--skip-validation` | Skip Access Analyzer calls (offline mode) |
| `--events-file path.jsonl` | Feed pre-collected events (e.g. from Athena/CloudWatch Insights) instead of hitting the LookupEvents API |
| `-v` | Verbose logging |

## Output files (in `--out-dir`)

| File | Contents |
|---|---|
| `current_policy.json` | Snapshot of the role's inline + attached policies at audit time |
| `refactored_policy.json` | Proposed least-privilege policy document |
| `validation_findings.json` | Raw Access Analyzer findings + CheckNoNewAccess result |
| `summary.json` | Machine-readable audit summary |
| `summary.txt` | Human-readable audit summary (also printed to stdout) |

## Required IAM permissions for the auditor principal

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": [
        "cloudtrail:LookupEvents",
        "iam:GetRole",
        "iam:ListRolePolicies",
        "iam:GetRolePolicy",
        "iam:ListAttachedRolePolicies",
        "iam:GetPolicy",
        "iam:GetPolicyVersion",
        "access-analyzer:ValidatePolicy",
        "access-analyzer:CheckNoNewAccess",
        "ec2:DescribeRegions"
      ],
      "Resource": "*"
    }
  ]
}
```

## Design notes & caveats

- **CloudTrail 90-day window**: `LookupEvents` only surfaces the last 90 days of management events. For a longer window, pipe Athena/S3-archived CloudTrail records into `--events-file` as JSONL (one CloudTrail record per line).
- **Data events**: S3/Lambda/DynamoDB data-plane events are only visible if the trail is explicitly configured to log them. If your reads look "invisible," check trail data-event selectors.
- **Access-denied calls are ignored** — a `403` proves the caller *tried* to do X, not that the policy permits it. Only successful calls count as evidence of legitimate use.
- **Deny statements are always preserved** verbatim — the auditor never removes a Deny even if it appears "unused."
- **Wildcard resource fallback**: services that don't populate the `resources` block in CloudTrail (e.g. `sts:GetCallerIdentity`, most `iam` reads) get a `"*"` resource in the refactored policy. This matches how AWS itself documents those actions.
- **CheckNoNewAccess** is a critical safety net: it proves the refactored policy is a strict subset of the original. If it returns `FAIL`, the refactor accidentally *grew* permissions — inspect before applying.
- **CI gating**: the script exits `1` if Access Analyzer surfaces any `ERROR` or `SECURITY_WARNING` findings, so you can wire it directly into a pipeline.

## Offline dry-run

The repo ships with sample events and a sample bloated policy so you can see
the whole pipeline without any AWS credentials:

```bash
python tests/test_dry_run.py
```

Expected output shows the original 8 action patterns reduced to 5 concrete
actions, wildcards expanded, and the `Deny` statement preserved.

## Module layout

```
iam_auditor/
  cloudtrail_collector.py   # LookupEvents across regions -> TrailEvent
  policy_loader.py          # Inline + managed policy retrieval
  action_matcher.py         # Wildcard-aware usage matcher
  policy_builder.py         # Refactored policy document builder
  validator.py              # Access Analyzer ValidatePolicy + CheckNoNewAccess
  reporter.py               # Summary report (text + JSON)
audit_iam_role.py           # CLI entry point
tests/test_dry_run.py       # Offline end-to-end smoke test
```

## Recommended workflow

1. Run the auditor in **report-only** mode against a role you suspect is over-privileged.
2. Review `summary.txt` — pay attention to `Statements with no observed usage`.
3. Inspect `refactored_policy.json` and, importantly, `validation_findings.json`.
4. Confirm `CheckNoNewAccess: PASS`.
5. Attach the refactored policy in a **shadow role** first; run traffic through it for a week.
6. Cut over the original role once the shadow role shows zero `AccessDenied` events.
