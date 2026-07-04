# OTel telemetry pipeline permission probe

`otel_probe.py` simulates a GitHub Actions runner pushing telemetry through
the provisioned IAM role. It exercises every AWS destination the OTel policy
touches (CloudWatch Logs, S3, X-Ray, AMP remote_write) plus a few negative
probes, and writes a diagnostic report that flags any `403 Forbidden` /
`AccessDenied` so you can prove least-privilege parity **before** you flip
the pipeline on in production.

## Probes

| # | Service | Action | Expected |
|---|---------|--------|----------|
| 1 | logs | `CreateLogStream` on the traces group | OK |
| 2 | logs | `PutLogEvents` on the traces group | OK |
| 3 | logs | `CreateLogStream` on the metrics group | OK |
| 4 | logs | `PutLogEvents` on the metrics group | OK |
| 5 | s3   | `PutObject` under `otel/` prefix | OK |
| 6 | s3   | `ListBucket` with `prefix=otel/` | OK |
| 7 | s3   | `PutObject` under `nope/` prefix | OK by default, DENY with `--strict-s3-prefix` |
| 8 | s3   | `ListBucket` with a different prefix | DENY (module ties `s3:ListBucket` to `s3:prefix=otel/*`) |
| 9 | xray | `PutTraceSegments` | OK |
| 10 | xray | `PutTelemetryRecords` | OK |
| 11 | aps  | `RemoteWrite` (SigV4-signed Prometheus write) | OK |
| 12 | iam  | `ListRoles` (negative — must be denied) | DENY |

## How the credentials line up with the runner

The script accepts credentials three ways, matching how a real GitHub runner
resolves them:

1. **`AWS_WEB_IDENTITY_TOKEN_FILE` + `AWS_ROLE_ARN`** — exactly what
   `aws-actions/configure-aws-credentials` exports on the runner. boto3
   picks this up natively; you don't pass anything on the CLI.
2. **`--assume-role-arn <role>`** — for local dev where you want to jump
   into the OTel role from an admin session. Uses `sts:AssumeRole`.
3. **Ambient session** (env vars, `~/.aws`, EC2/ECS role). The script
   refuses to run against `--env production` this way unless you also pass
   `--allow-ambient`, so you can't fat-finger a laptop session against prod.

## Local usage

```bash
cd scripts/otel_probe
cp .env.example .env.staging   # fill in real values
make probe-staging
```

Reports land in `./reports/otel-probe-<env>-<UTC>.{json,md}`. If any probe
came back `403` / `AccessDenied`, an extra `-403.md` file is written with
just the failures and a parsed hint of the missing action (e.g.
`aps:RemoteWrite`) so you can paste it straight into a policy change ticket.

## From a real GitHub runner

Add a job that assumes the role via OIDC and then invokes the script — this
is the truest end-to-end check because the credentials arrive through the
same path production traffic will use.

```yaml
- uses: aws-actions/configure-aws-credentials@v4
  with:
    role-to-assume: ${{ vars.STAGING_ROLE_ARN }}
    aws-region: us-east-1
- run: |
    pip install -r scripts/otel_probe/requirements.txt
    python scripts/otel_probe/otel_probe.py \
      --region us-east-1 --env staging \
      --traces-log-group  /otel/traces \
      --metrics-log-group /otel/metrics \
      --bucket ${{ vars.TELEMETRY_BUCKET }} \
      --amp-endpoint ${{ vars.AMP_REMOTE_WRITE_URL }} \
      --report-dir ./reports
- uses: actions/upload-artifact@v4
  with:
    name: otel-probe-report
    path: reports/
```

## What "PASS" means

The script exits 0 only when every probe's actual outcome matches its
expected outcome. That means:

- All 10 positive probes returned success — the role has enough permission
  to run the pipeline.
- All negative probes returned `AccessDenied` — the role has **only** those
  permissions and nothing else. Any unexpected allow (e.g. `iam:ListRoles`
  succeeding) is flagged in the summary as an over-permissive grant that
  should be tightened before production ingest is enabled.

If probe 11 fails with HTTP 400 (not 403), it means the SigV4 signature was
accepted but the body was malformed — likely because `python-snappy` isn't
installed. That still validates the `aps:RemoteWrite` permission, so the
script reports it as a pass and notes the reason. Install
`python-snappy` for a true end-to-end validation of the AMP path.
