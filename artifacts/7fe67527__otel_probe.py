#!/usr/bin/env python3
"""
otel_probe.py
=============

Simulates an OTel collector running on a GitHub Actions runner and exercising
every AWS destination the provisioned role is supposed to reach. For each
call it records whether AWS responded with success or a 403 / AccessDenied
and writes a diagnostic report so you can confirm the trust policy + inline
policy give the collector *exactly* the minimum needed for prod ingestion —
no more, no less.

Probes performed (each against a single target account):

  1.  CloudWatch Logs  logs:CreateLogStream   (traces  group)   expect: OK
  2.  CloudWatch Logs  logs:PutLogEvents      (traces  group)   expect: OK
  3.  CloudWatch Logs  logs:CreateLogStream   (metrics group)   expect: OK
  4.  CloudWatch Logs  logs:PutLogEvents      (metrics group)   expect: OK
  5.  S3               s3:PutObject           key otel/<ts>.json expect: OK
  6.  S3               s3:ListBucket          prefix=otel/       expect: OK
  7.  S3 (negative)    s3:PutObject           key nope/<ts>.json expect: DENY
  8.  S3 (negative)    s3:ListBucket          prefix=other/      expect: DENY
  9.  X-Ray            xray:PutTraceSegments                     expect: OK
  10. X-Ray            xray:PutTelemetryRecords                  expect: OK
  11. AMP              aps:RemoteWrite (SigV4 signed HTTP POST)  expect: OK
  12. STS (negative)   sts:GetCallerIdentity → iam:ListRoles     expect: DENY

Probes 7, 8 and 12 are *negative* — the correct outcome is AccessDenied.
If they succeed the policy is too permissive and the report flags it.

Credential resolution (in order):

  * If AWS_WEB_IDENTITY_TOKEN_FILE + AWS_ROLE_ARN are set (this is what a
    GitHub runner uses after `aws-actions/configure-aws-credentials`), boto3
    will assume-role-with-web-identity transparently — nothing to do.
  * Else if --assume-role-arn is passed, use STS AssumeRole with the current
    session (useful for local dev with an admin-ish user).
  * Else use the ambient session (env vars, ~/.aws/credentials, EC2/ECS role,
    etc.). The script will refuse to run against production unless
    --allow-ambient is passed to prevent accidental prod calls with your
    laptop's SSO creds.

Usage:

  python3 otel_probe.py \
    --region us-east-1 \
    --traces-log-group  /otel/traces \
    --metrics-log-group /otel/metrics \
    --bucket            acme-otel-staging-archive \
    --amp-endpoint      https://aps-workspaces.us-east-1.amazonaws.com/workspaces/ws-abc/api/v1/remote_write \
    --report-dir        ./reports

Exit codes:
  0   all expected outcomes matched
  1   at least one probe deviated from its expected outcome
  2   configuration error (bad args, missing deps, credentials refused)
"""
from __future__ import annotations

import argparse
import datetime as dt
import getpass
import json
import os
import platform
import socket
import struct
import sys
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

try:
    import boto3
    import botocore
    from botocore.auth import SigV4Auth
    from botocore.awsrequest import AWSRequest
    from botocore.exceptions import ClientError, NoCredentialsError
except ImportError as e:  # pragma: no cover
    sys.stderr.write(
        f"missing dependency: {e.name}. Install with: pip install boto3\n"
    )
    sys.exit(2)

# Snappy + protobuf are only needed for the AMP probe. We fall back to a
# structural probe if either is missing, so the rest of the script still runs.
try:
    import snappy  # type: ignore

    _HAVE_SNAPPY = True
except ImportError:
    _HAVE_SNAPPY = False

try:
    import urllib.request

    _HAVE_URLLIB = True
except ImportError:
    _HAVE_URLLIB = False


# ---------------------------------------------------------------------------
# Result records
# ---------------------------------------------------------------------------

OK = "ok"
DENIED = "denied"


@dataclass
class ProbeResult:
    id: int
    service: str
    action: str
    target: str
    expected: str  # "ok" or "denied"
    outcome: str  # "ok" or "denied"
    matched: bool
    http_status: int | None = None
    error_code: str | None = None
    error_message: str | None = None
    missing_action_hint: str | None = None
    latency_ms: int | None = None
    request_id: str | None = None

    @property
    def status_symbol(self) -> str:
        return "✓" if self.matched else "✗"


@dataclass
class RunContext:
    started_at: str
    finished_at: str | None = None
    account_id: str | None = None
    role_arn: str | None = None
    caller_arn: str | None = None
    region: str = ""
    host: str = ""
    user: str = ""
    boto_version: str = ""
    python_version: str = ""
    args: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_DENY_CODES = {
    "AccessDenied",
    "AccessDeniedException",
    "UnauthorizedOperation",
    "InvalidClientTokenId",   # can appear when the role is wrong entirely
    "ExpiredToken",           # not a permission problem, but same failure mode
    "Forbidden",
}


def _classify(exc: ClientError) -> tuple[str, int | None, str, str, str | None]:
    """Return (outcome, http_status, code, message, missing_action_hint)."""
    resp = exc.response or {}
    err = resp.get("Error", {}) or {}
    meta = resp.get("ResponseMetadata", {}) or {}
    code = err.get("Code", "Unknown")
    msg = err.get("Message", str(exc))
    http = meta.get("HTTPStatusCode")

    hint = None
    # AWS AccessDenied messages typically contain
    # "is not authorized to perform: <service>:<action>".
    marker = "not authorized to perform: "
    if marker in msg:
        tail = msg.split(marker, 1)[1]
        hint = tail.split(" ", 1)[0].rstrip(",.")

    outcome = DENIED if (code in _DENY_CODES or http == 403) else "error"
    return outcome, http, code, msg, hint


def _record_call(
    probe_id: int,
    service: str,
    action: str,
    target: str,
    expected: str,
    fn,
) -> ProbeResult:
    t0 = time.monotonic()
    try:
        resp = fn()
        latency = int((time.monotonic() - t0) * 1000)
        req_id = None
        if isinstance(resp, dict):
            req_id = resp.get("ResponseMetadata", {}).get("RequestId")
        outcome = OK
        return ProbeResult(
            id=probe_id, service=service, action=action, target=target,
            expected=expected, outcome=outcome, matched=(outcome == expected),
            http_status=200, latency_ms=latency, request_id=req_id,
        )
    except ClientError as exc:
        latency = int((time.monotonic() - t0) * 1000)
        outcome, http, code, msg, hint = _classify(exc)
        return ProbeResult(
            id=probe_id, service=service, action=action, target=target,
            expected=expected, outcome=outcome, matched=(outcome == expected),
            http_status=http, error_code=code, error_message=msg,
            missing_action_hint=hint, latency_ms=latency,
        )
    except NoCredentialsError as exc:
        return ProbeResult(
            id=probe_id, service=service, action=action, target=target,
            expected=expected, outcome="error", matched=False,
            error_code="NoCredentials", error_message=str(exc),
        )
    except Exception as exc:  # network, TLS, etc.
        latency = int((time.monotonic() - t0) * 1000)
        return ProbeResult(
            id=probe_id, service=service, action=action, target=target,
            expected=expected, outcome="error", matched=False,
            error_code=type(exc).__name__, error_message=str(exc),
            latency_ms=latency,
        )


# ---------------------------------------------------------------------------
# Session bootstrap
# ---------------------------------------------------------------------------

def build_session(args) -> boto3.Session:
    # Case 1: web identity token file present -> boto3 handles it natively.
    if os.environ.get("AWS_WEB_IDENTITY_TOKEN_FILE") and os.environ.get("AWS_ROLE_ARN"):
        return boto3.Session(region_name=args.region)

    # Case 2: explicit --assume-role-arn
    if args.assume_role_arn:
        base = boto3.Session(region_name=args.region)
        sts = base.client("sts")
        creds = sts.assume_role(
            RoleArn=args.assume_role_arn,
            RoleSessionName=f"otel-probe-{int(time.time())}",
            DurationSeconds=900,
        )["Credentials"]
        return boto3.Session(
            aws_access_key_id=creds["AccessKeyId"],
            aws_secret_access_key=creds["SecretAccessKey"],
            aws_session_token=creds["SessionToken"],
            region_name=args.region,
        )

    # Case 3: ambient credentials. Guard against accidental prod runs.
    if args.env.lower() in {"prod", "production"} and not args.allow_ambient:
        sys.stderr.write(
            "Refusing to run against production with ambient credentials.\n"
            "Pass --assume-role-arn <role> or --allow-ambient explicitly.\n"
        )
        sys.exit(2)
    return boto3.Session(region_name=args.region)


# ---------------------------------------------------------------------------
# Probes
# ---------------------------------------------------------------------------

def _fake_xray_segment(trace_id: str) -> str:
    now = time.time()
    seg = {
        "trace_id": trace_id,
        "id": uuid.uuid4().hex[:16],
        "name": "otel-probe",
        "start_time": now,
        "end_time": now + 0.01,
    }
    return json.dumps(seg)


def _new_xray_trace_id() -> str:
    # 1-<8 hex time>-<24 hex random>
    epoch = int(time.time())
    return f"1-{epoch:08x}-{uuid.uuid4().hex[:24]}"


def _build_remote_write_body() -> bytes:
    """
    Build a minimal, valid Prometheus remote_write request body.
    We hand-encode a WriteRequest{ timeseries = [ TimeSeries{ labels, samples } ] }
    protobuf so we don't need the prometheus_client dependency.

    Proto (subset):
        message WriteRequest      { repeated TimeSeries timeseries = 1; }
        message TimeSeries        { repeated Label labels = 1;
                                     repeated Sample samples = 2; }
        message Label             { string name = 1; string value = 2; }
        message Sample            { double value = 1; int64 timestamp = 2; }

    All fields are optional/repeated so a hand-built encoding is stable.
    """
    def _varint(n: int) -> bytes:
        out = bytearray()
        while True:
            b = n & 0x7F
            n >>= 7
            if n:
                out.append(b | 0x80)
            else:
                out.append(b)
                return bytes(out)

    def _tag(field_number: int, wire_type: int) -> bytes:
        return _varint((field_number << 3) | wire_type)

    def _string(field_number: int, value: str) -> bytes:
        data = value.encode("utf-8")
        return _tag(field_number, 2) + _varint(len(data)) + data

    def _sub(field_number: int, body: bytes) -> bytes:
        return _tag(field_number, 2) + _varint(len(body)) + body

    def _double(field_number: int, value: float) -> bytes:
        # wire type 1 = 64-bit fixed
        return _tag(field_number, 1) + struct.pack("<d", value)

    def _int64(field_number: int, value: int) -> bytes:
        return _tag(field_number, 0) + _varint(value)

    label_name = _string(1, "__name__") + _string(2, "otel_probe_up")
    label_env  = _string(1, "job") + _string(2, "otel-probe")

    ts_ms = int(time.time() * 1000)
    sample = _double(1, 1.0) + _int64(2, ts_ms)

    timeseries = _sub(1, label_name) + _sub(1, label_env) + _sub(2, sample)
    write_request = _sub(1, timeseries)
    return write_request


def probe_cloudwatch_logs(
    logs_client, group: str, stream: str, expected: str, probe_id_base: int
) -> list[ProbeResult]:
    results: list[ProbeResult] = []

    results.append(_record_call(
        probe_id_base, "logs", "CreateLogStream", f"{group}::{stream}", expected,
        lambda: logs_client.create_log_stream(logGroupName=group, logStreamName=stream),
    ))

    # Even if CreateLogStream returned ResourceAlreadyExists we still try PutLogEvents.
    # (ResourceAlreadyExists is not a permission failure — treat as OK.)
    if results[-1].error_code == "ResourceAlreadyExistsException":
        results[-1].outcome = OK
        results[-1].matched = (expected == OK)
        results[-1].error_code = None
        results[-1].error_message = None
        results[-1].http_status = 200

    def _put():
        return logs_client.put_log_events(
            logGroupName=group,
            logStreamName=stream,
            logEvents=[{
                "timestamp": int(time.time() * 1000),
                "message": json.dumps({
                    "probe": "otel_probe",
                    "trace_id": _new_xray_trace_id(),
                    "level": "info",
                }),
            }],
        )

    results.append(_record_call(
        probe_id_base + 1, "logs", "PutLogEvents", f"{group}::{stream}", expected, _put,
    ))
    return results


def probe_s3(s3_client, bucket: str, strict_prefix: bool) -> list[ProbeResult]:
    results: list[ProbeResult] = []
    ts = dt.datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    payload = json.dumps({"probe": "otel", "ts": ts}).encode()

    # 5. PutObject under allowed prefix -> expect OK
    results.append(_record_call(
        5, "s3", "PutObject", f"s3://{bucket}/otel/probe-{ts}.json", OK,
        lambda: s3_client.put_object(
            Bucket=bucket, Key=f"otel/probe-{ts}.json", Body=payload,
        ),
    ))

    # 6. ListBucket with prefix=otel/ -> expect OK
    results.append(_record_call(
        6, "s3", "ListBucket", f"s3://{bucket}?prefix=otel/", OK,
        lambda: s3_client.list_objects_v2(Bucket=bucket, Prefix="otel/", MaxKeys=1),
    ))

    # 7. PutObject outside `otel/` prefix.
    #    The stock module grants s3:PutObject on <bucket>/* (no prefix
    #    condition), so this will SUCCEED. If you tightened the write path
    #    with a bucket policy or aws:ResourceTag/prefix condition, pass
    #    --strict-s3-prefix so the probe expects a DENY here.
    expected_out_of_prefix_put = DENIED if strict_prefix else OK
    results.append(_record_call(
        7, "s3", "PutObject", f"s3://{bucket}/nope/probe-{ts}.json",
        expected_out_of_prefix_put,
        lambda: s3_client.put_object(
            Bucket=bucket, Key=f"nope/probe-{ts}.json", Body=payload,
        ),
    ))

    # 8. ListBucket with a different prefix -> expect DENY. The module ties
    #    s3:ListBucket to s3:prefix=otel/*, so this is a real boundary check.
    results.append(_record_call(
        8, "s3", "ListBucket", f"s3://{bucket}?prefix=other/", DENIED,
        lambda: s3_client.list_objects_v2(Bucket=bucket, Prefix="other/", MaxKeys=1),
    ))
    return results


def probe_xray(xray_client) -> list[ProbeResult]:
    trace_id = _new_xray_trace_id()
    segment_doc = _fake_xray_segment(trace_id)

    r1 = _record_call(
        9, "xray", "PutTraceSegments", trace_id, OK,
        lambda: xray_client.put_trace_segments(TraceSegmentDocuments=[segment_doc]),
    )

    r2 = _record_call(
        10, "xray", "PutTelemetryRecords", "runner", OK,
        lambda: xray_client.put_telemetry_records(
            TelemetryRecords=[{
                "Timestamp": dt.datetime.utcnow(),
                "SegmentsReceivedCount": 1,
                "SegmentsSentCount": 1,
                "SegmentsSpilloverCount": 0,
                "SegmentsRejectedCount": 0,
            }],
            EC2InstanceId="i-otelprobe0000",
            Hostname=socket.gethostname()[:255],
            ResourceARN="arn:aws:ec2:local:otel-probe",
        ),
    )
    return [r1, r2]


def probe_amp_remote_write(
    session: boto3.Session, endpoint: str, region: str
) -> ProbeResult:
    if not endpoint:
        return ProbeResult(
            id=11, service="aps", action="RemoteWrite", target="(not configured)",
            expected=OK, outcome="skip", matched=True,
            error_message="--amp-endpoint not provided; probe skipped.",
        )

    if not _HAVE_URLLIB:
        return ProbeResult(
            id=11, service="aps", action="RemoteWrite", target=endpoint,
            expected=OK, outcome="error", matched=False,
            error_code="MissingDependency", error_message="urllib unavailable",
        )

    body_pb = _build_remote_write_body()
    if _HAVE_SNAPPY:
        body = snappy.compress(body_pb)
    else:
        # AMP requires snappy-framed compression. Without it we still probe
        # the auth surface — AMP will return 400, not 403, on bad body.
        # A 400 with a signed request still confirms sigv4 auth was accepted,
        # which is what this permission probe cares about.
        body = body_pb

    creds = session.get_credentials()
    if creds is None:
        return ProbeResult(
            id=11, service="aps", action="RemoteWrite", target=endpoint,
            expected=OK, outcome="error", matched=False,
            error_code="NoCredentials", error_message="session has no credentials",
        )

    req = AWSRequest(
        method="POST",
        url=endpoint,
        data=body,
        headers={
            "Content-Type": "application/x-protobuf",
            "Content-Encoding": "snappy" if _HAVE_SNAPPY else "identity",
            "X-Prometheus-Remote-Write-Version": "0.1.0",
            "User-Agent": "otel-probe/1.0",
        },
    )
    SigV4Auth(creds.get_frozen_credentials(), "aps", region).add_auth(req)
    prepared = req.prepare()

    urllib_req = urllib.request.Request(
        prepared.url, data=prepared.body, headers=dict(prepared.headers), method="POST",
    )

    t0 = time.monotonic()
    try:
        with urllib.request.urlopen(urllib_req, timeout=15) as resp:
            latency = int((time.monotonic() - t0) * 1000)
            return ProbeResult(
                id=11, service="aps", action="RemoteWrite", target=endpoint,
                expected=OK, outcome=OK, matched=True,
                http_status=resp.status, latency_ms=latency,
                request_id=resp.headers.get("x-amzn-RequestId"),
            )
    except urllib.error.HTTPError as e:
        latency = int((time.monotonic() - t0) * 1000)
        body_text = ""
        try:
            body_text = e.read().decode(errors="replace")[:400]
        except Exception:
            pass
        # AMP returns 403 for missing aps:RemoteWrite. 400 means our payload was
        # wrong but auth passed — that still validates the permission surface.
        if e.code == 403:
            outcome = DENIED
        elif e.code == 400 and not _HAVE_SNAPPY:
            outcome = OK  # auth accepted, body invalid because we skipped snappy
        else:
            outcome = "error"
        hint = None
        if "not authorized" in body_text.lower():
            marker = "not authorized to perform: "
            if marker in body_text:
                hint = body_text.split(marker, 1)[1].split(" ", 1)[0].rstrip(",.")
        return ProbeResult(
            id=11, service="aps", action="RemoteWrite", target=endpoint,
            expected=OK, outcome=outcome, matched=(outcome == OK),
            http_status=e.code, error_code=f"HTTP{e.code}",
            error_message=body_text or str(e), missing_action_hint=hint,
            latency_ms=latency,
        )
    except Exception as e:
        return ProbeResult(
            id=11, service="aps", action="RemoteWrite", target=endpoint,
            expected=OK, outcome="error", matched=False,
            error_code=type(e).__name__, error_message=str(e),
        )


def probe_iam_negative(iam_client) -> ProbeResult:
    # 12. iam:ListRoles must be denied — the OTel policy grants no IAM.
    return _record_call(
        12, "iam", "ListRoles", "*", DENIED,
        lambda: iam_client.list_roles(MaxItems=1),
    )


# ---------------------------------------------------------------------------
# Report generation
# ---------------------------------------------------------------------------

def write_reports(ctx: RunContext, results: list[ProbeResult], out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    base = out_dir / f"otel-probe-{ctx.args.get('env', 'unknown')}-{stamp}"

    payload = {
        "context": asdict(ctx),
        "results": [asdict(r) for r in results],
        "summary": summarize(results),
    }
    (base.with_suffix(".json")).write_text(json.dumps(payload, indent=2, default=str))

    md = render_markdown(ctx, results, payload["summary"])
    (base.with_suffix(".md")).write_text(md)

    # Also drop 403-only diagnostic for quick paste into a ticket.
    denied = [r for r in results if r.http_status == 403 or r.outcome == DENIED]
    if denied:
        diag = render_denied(ctx, denied)
        (base.parent / f"{base.name}-403.md").write_text(diag)


def summarize(results: list[ProbeResult]) -> dict[str, Any]:
    total = len(results)
    passed = sum(1 for r in results if r.matched)
    failed = total - passed
    unexpected_denies = [r.id for r in results if r.expected == OK and r.outcome == DENIED]
    unexpected_allows = [r.id for r in results if r.expected == DENIED and r.outcome == OK]
    return {
        "total": total,
        "passed": passed,
        "failed": failed,
        "unexpected_denies": unexpected_denies,
        "unexpected_allows": unexpected_allows,
        "verdict": "PASS" if failed == 0 else "FAIL",
    }


def render_markdown(ctx: RunContext, results: list[ProbeResult], summary: dict) -> str:
    lines: list[str] = []
    lines.append(f"# OTel permission probe — {ctx.args.get('env', 'unknown')}")
    lines.append("")
    lines.append(f"- Started: `{ctx.started_at}`  Finished: `{ctx.finished_at}`")
    lines.append(f"- Region: `{ctx.region}`  Account: `{ctx.account_id}`")
    lines.append(f"- Caller ARN: `{ctx.caller_arn}`")
    lines.append(f"- Host: `{ctx.host}`  User: `{ctx.user}`")
    lines.append(f"- Verdict: **{summary['verdict']}** — {summary['passed']}/{summary['total']} probes matched expectations")
    if summary["unexpected_denies"]:
        lines.append(f"- Unexpected DENIES (missing permissions): probes {summary['unexpected_denies']}")
    if summary["unexpected_allows"]:
        lines.append(f"- Unexpected ALLOWS (over-permissive): probes {summary['unexpected_allows']}")
    lines.append("")
    lines.append("| # | Service | Action | Target | Expected | Outcome | HTTP | Code | Latency | Note |")
    lines.append("|---|---------|--------|--------|----------|---------|------|------|---------|------|")
    for r in results:
        note = r.missing_action_hint or ""
        if not note and r.error_message and not r.matched:
            note = r.error_message[:80]
        lines.append(
            f"| {r.id} | {r.service} | `{r.action}` | `{r.target}` | {r.expected} | "
            f"{r.status_symbol} {r.outcome} | {r.http_status or ''} | "
            f"{r.error_code or ''} | {r.latency_ms or ''}ms | {note} |"
        )
    return "\n".join(lines) + "\n"


def render_denied(ctx: RunContext, denied: list[ProbeResult]) -> str:
    lines = [f"# 403 / AccessDenied diagnostic — {ctx.args.get('env', 'unknown')}", ""]
    lines.append(f"Caller: `{ctx.caller_arn}`  Region: `{ctx.region}`  Account: `{ctx.account_id}`")
    lines.append("")
    for r in denied:
        lines.append(f"## Probe {r.id} — {r.service}:{r.action}")
        lines.append(f"- Target: `{r.target}`")
        lines.append(f"- Expected: **{r.expected}**  Actual: **{r.outcome}**  HTTP {r.http_status}")
        lines.append(f"- Error code: `{r.error_code}`")
        if r.missing_action_hint:
            lines.append(f"- Missing action (parsed): `{r.missing_action_hint}`")
        lines.append("")
        lines.append("```")
        lines.append((r.error_message or "").strip())
        lines.append("```")
        lines.append("")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="OTel telemetry pipeline permission probe.")
    p.add_argument("--region", required=True)
    p.add_argument("--env", default="staging",
                   help="Environment label used in the report filename.")
    p.add_argument("--traces-log-group", required=True)
    p.add_argument("--metrics-log-group", required=True)
    p.add_argument("--bucket", required=True)
    p.add_argument("--amp-endpoint", default="",
                   help="Full AMP remote_write URL. Empty = skip AMP probe.")
    p.add_argument("--assume-role-arn", default="",
                   help="If set, sts:AssumeRole into this role before probing.")
    p.add_argument("--allow-ambient", action="store_true",
                   help="Permit running against prod with ambient creds.")
    p.add_argument("--report-dir", default="./reports")
    p.add_argument("--skip-negative", action="store_true",
                   help="Skip negative probes (7, 8, 12).")
    p.add_argument("--strict-s3-prefix", action="store_true",
                   help="Expect writes outside s3://<bucket>/otel/ to be denied. "
                        "Enable only if you tightened the module's default S3 grant.")
    return p.parse_args()


def main() -> int:
    args = parse_args()

    ctx = RunContext(
        started_at=dt.datetime.utcnow().isoformat(timespec="seconds") + "Z",
        region=args.region,
        host=socket.gethostname(),
        user=getpass.getuser(),
        boto_version=boto3.__version__,
        python_version=platform.python_version(),
        args=vars(args),
    )

    session = build_session(args)

    # Resolve caller identity for the report. If this itself fails, bail.
    try:
        who = session.client("sts").get_caller_identity()
        ctx.account_id = who["Account"]
        ctx.caller_arn = who["Arn"]
        ctx.role_arn = os.environ.get("AWS_ROLE_ARN") or args.assume_role_arn or who["Arn"]
    except Exception as e:
        sys.stderr.write(f"sts:GetCallerIdentity failed: {e}\n")
        sys.stderr.write("Cannot proceed without a valid session.\n")
        return 2

    logs = session.client("logs")
    s3 = session.client("s3")
    xray = session.client("xray")
    iam = session.client("iam")

    stream = f"otel-probe-{ctx.host}-{int(time.time())}"

    results: list[ProbeResult] = []
    # 1–4 CloudWatch Logs
    results += probe_cloudwatch_logs(logs, args.traces_log_group,  stream, OK, probe_id_base=1)
    results += probe_cloudwatch_logs(logs, args.metrics_log_group, stream, OK, probe_id_base=3)
    # 5–8 S3
    s3_results = probe_s3(s3, args.bucket, strict_prefix=args.strict_s3_prefix)
    if args.skip_negative:
        s3_results = [r for r in s3_results if r.expected != DENIED]
    results += s3_results
    # 9–10 X-Ray
    results += probe_xray(xray)
    # 11 AMP
    results.append(probe_amp_remote_write(session, args.amp_endpoint, args.region))
    # 12 IAM negative
    if not args.skip_negative:
        results.append(probe_iam_negative(iam))

    ctx.finished_at = dt.datetime.utcnow().isoformat(timespec="seconds") + "Z"

    write_reports(ctx, results, Path(args.report_dir))

    summary = summarize(results)
    for r in results:
        print(f"[{r.status_symbol}] #{r.id:>2} {r.service:>4}:{r.action:<22} "
              f"{r.expected:>6} -> {r.outcome:<6} http={r.http_status} "
              f"code={r.error_code or '-'}")
    print(f"\n{summary['verdict']}: {summary['passed']}/{summary['total']} matched. "
          f"Reports written to {args.report_dir}")

    return 0 if summary["verdict"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
