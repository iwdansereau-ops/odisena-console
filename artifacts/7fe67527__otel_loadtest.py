#!/usr/bin/env python3
"""
otel_loadtest.py
================

Load-injection harness for the OTel telemetry pipeline. Ramps
`xray:PutTraceSegments` and `aps:RemoteWrite` from 1x through 10x a stated
baseline peak throughput, records per-second outcomes (2xx / 403 / 429 /
5xx / other), correlates against AWS-side signals (X-Ray & AMP CloudWatch
metrics, optional Logs Insights queries), and writes a
throughput-vs-errors sensitivity report plus a pre-filled GitHub issue
draft with recommended quota increases and backoff-config changes.

Design highlights
-----------------
* asyncio dispatcher, work-stealing per-second token buckets so achieved
  RPS tracks the schedule regardless of tail latency.
* AMP path uses raw aiohttp with SigV4 signing (via botocore) so we get
  exact HTTP status codes and Retry-After headers — the boto3 client
  wraps these into ClientError and drops the header.
* X-Ray uses boto3 in a thread pool (no first-party async client).
* Hard safety rails: total-request cap, wallclock cap, per-stage kill
  switch (`--kill-switch-file`), and a `--dry-run` that only prints the
  plan.
* AMP docs warn about doubling-above-baseline throttling; between stages
  we run a `--stage-cooldown-sec` gap so we don't collide with AMP's
  30-min rolling baseline. Defaults chosen to be safe on staging.

Exit codes:
    0   ramp completed and observed error rate stayed under
        `--error-budget-pct` at every stage.
    1   ramp completed but at least one stage exceeded the budget.
    2   configuration error / missing credentials / kill-switch triggered.
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import datetime as dt
import getpass
import json
import math
import os
import platform
import random
import signal
import socket
import statistics
import struct
import sys
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

try:
    import aiohttp
    import boto3
    import botocore
    from botocore.auth import SigV4Auth
    from botocore.awsrequest import AWSRequest
    from botocore.exceptions import ClientError, NoCredentialsError
except ImportError as e:  # pragma: no cover
    sys.stderr.write(
        f"missing dependency: {e.name}. Install with: pip install -r requirements.txt\n"
    )
    sys.exit(2)

try:
    import snappy  # type: ignore

    _HAVE_SNAPPY = True
except ImportError:
    _HAVE_SNAPPY = False


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------

@dataclass
class Outcome:
    ts: float
    service: str            # "xray" or "amp"
    stage: int              # 1..N
    multiplier: float       # e.g. 3.0 = 3x baseline
    latency_ms: float
    http_status: int | None
    error_code: str | None  # AWS error code or HTTP-code label
    retry_after_ms: int | None = None


@dataclass
class StageStats:
    stage: int
    multiplier: float
    xray_target_tps: float
    amp_target_samples_per_sec: float
    started_at: str
    finished_at: str
    xray_sent: int = 0
    xray_ok: int = 0
    xray_403: int = 0
    xray_429: int = 0
    xray_5xx: int = 0
    xray_other_err: int = 0
    xray_p50_ms: float = 0.0
    xray_p99_ms: float = 0.0
    amp_sent: int = 0
    amp_ok: int = 0
    amp_403: int = 0
    amp_429: int = 0
    amp_5xx: int = 0
    amp_other_err: int = 0
    amp_p50_ms: float = 0.0
    amp_p99_ms: float = 0.0
    xray_achieved_tps: float = 0.0
    amp_achieved_samples_per_sec: float = 0.0
    # Populated by the correlation pass:
    aws_metrics: dict[str, Any] = field(default_factory=dict)

    @property
    def xray_error_rate(self) -> float:
        return 0.0 if self.xray_sent == 0 else 1 - (self.xray_ok / self.xray_sent)

    @property
    def amp_error_rate(self) -> float:
        return 0.0 if self.amp_sent == 0 else 1 - (self.amp_ok / self.amp_sent)


# ---------------------------------------------------------------------------
# Fixture builders
# ---------------------------------------------------------------------------

def _new_trace_id() -> str:
    return f"1-{int(time.time()):08x}-{uuid.uuid4().hex[:24]}"


def _build_xray_segment_batch(batch_size: int) -> list[str]:
    """One PutTraceSegments call carries up to N segment documents."""
    docs = []
    now = time.time()
    for _ in range(batch_size):
        docs.append(json.dumps({
            "trace_id": _new_trace_id(),
            "id": uuid.uuid4().hex[:16],
            "name": "otel-loadtest",
            "start_time": now,
            "end_time": now + 0.005,
            "http": {"request": {"method": "GET", "url": "https://example/probe"},
                      "response": {"status": 200}},
        }))
    return docs


def _build_amp_write_body(samples_per_request: int) -> bytes:
    """
    Hand-encoded Prometheus WriteRequest protobuf with `samples_per_request`
    samples on a single time series. Snappy-compressed if available.
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

    def _tag(fn: int, wt: int) -> bytes: return _varint((fn << 3) | wt)
    def _str(fn: int, v: str) -> bytes:
        d = v.encode(); return _tag(fn, 2) + _varint(len(d)) + d
    def _sub(fn: int, body: bytes) -> bytes:
        return _tag(fn, 2) + _varint(len(body)) + body
    def _double(fn: int, v: float) -> bytes: return _tag(fn, 1) + struct.pack("<d", v)
    def _int64(fn: int, v: int) -> bytes: return _tag(fn, 0) + _varint(v)

    labels = _str(1, "__name__") + _str(2, "otel_loadtest_samples")
    labels += _str(1, "run_id") + _str(2, os.environ.get("LOADTEST_RUN_ID", "local"))
    labels_block = _sub(1, _str(1, "__name__") + _str(2, "otel_loadtest_samples")) + \
                    _sub(1, _str(1, "run_id") + _str(2, os.environ.get("LOADTEST_RUN_ID", "local")))

    # We must vary the timestamp per sample: identical (ts,series) is a 400.
    now_ms = int(time.time() * 1000)
    samples_block = b""
    for i in range(samples_per_request):
        samples_block += _sub(2, _double(1, random.random()) + _int64(2, now_ms + i))

    timeseries = labels_block + samples_block
    write_request = _sub(1, timeseries)
    return snappy.compress(write_request) if _HAVE_SNAPPY else write_request


# ---------------------------------------------------------------------------
# Sender coroutines
# ---------------------------------------------------------------------------

class XRaySender:
    def __init__(self, session: boto3.Session, batch_size: int, concurrency: int):
        self.client = session.client("xray")
        self.batch_size = batch_size
        self.sem = asyncio.Semaphore(concurrency)
        self.loop = asyncio.get_event_loop()

    async def send_one(self, stage: int, mult: float, out: list[Outcome]) -> None:
        docs = _build_xray_segment_batch(self.batch_size)
        async with self.sem:
            t0 = time.monotonic()
            try:
                resp = await self.loop.run_in_executor(
                    None,
                    lambda: self.client.put_trace_segments(TraceSegmentDocuments=docs),
                )
                latency = (time.monotonic() - t0) * 1000
                # X-Ray returns 200 even if individual segments were rejected — those
                # come back in UnprocessedTraceSegments. We count the call as OK but
                # record a soft error for the batch when any segment failed.
                unprocessed = resp.get("UnprocessedTraceSegments") or []
                if unprocessed:
                    out.append(Outcome(t0, "xray", stage, mult, latency, 200,
                                        f"UnprocessedSegments({len(unprocessed)})"))
                else:
                    out.append(Outcome(t0, "xray", stage, mult, latency, 200, None))
            except ClientError as e:
                latency = (time.monotonic() - t0) * 1000
                code = e.response.get("Error", {}).get("Code", "Unknown")
                http = e.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
                out.append(Outcome(t0, "xray", stage, mult, latency, http, code))
            except Exception as e:  # network etc.
                latency = (time.monotonic() - t0) * 1000
                out.append(Outcome(t0, "xray", stage, mult, latency, None, type(e).__name__))


class AmpSender:
    def __init__(self, session: boto3.Session, endpoint: str, region: str,
                 samples_per_request: int, concurrency: int):
        self.endpoint = endpoint
        self.region = region
        self.samples_per_request = samples_per_request
        self.creds = session.get_credentials()
        self.sem = asyncio.Semaphore(concurrency)
        self.session_kwargs = {
            "timeout": aiohttp.ClientTimeout(total=30, connect=5),
            "trust_env": True,
        }
        # HTTP session created lazily so we can share it across the run.
        self._http: aiohttp.ClientSession | None = None

    async def _http_session(self) -> aiohttp.ClientSession:
        if self._http is None or self._http.closed:
            self._http = aiohttp.ClientSession(**self.session_kwargs)
        return self._http

    async def close(self) -> None:
        if self._http and not self._http.closed:
            await self._http.close()

    def _sign(self, body: bytes) -> dict:
        req = AWSRequest(
            method="POST", url=self.endpoint, data=body,
            headers={
                "Content-Type": "application/x-protobuf",
                "Content-Encoding": "snappy" if _HAVE_SNAPPY else "identity",
                "X-Prometheus-Remote-Write-Version": "0.1.0",
                "User-Agent": "otel-loadtest/1.0",
            },
        )
        SigV4Auth(self.creds.get_frozen_credentials(), "aps", self.region).add_auth(req)
        return dict(req.prepare().headers)

    async def send_one(self, stage: int, mult: float, out: list[Outcome]) -> None:
        body = _build_amp_write_body(self.samples_per_request)
        headers = self._sign(body)
        http = await self._http_session()
        async with self.sem:
            t0 = time.monotonic()
            try:
                async with http.post(self.endpoint, data=body, headers=headers) as resp:
                    latency = (time.monotonic() - t0) * 1000
                    status = resp.status
                    err = None if 200 <= status < 300 else f"HTTP{status}"
                    ra = resp.headers.get("Retry-After")
                    retry_after_ms = None
                    if ra:
                        try:
                            retry_after_ms = int(float(ra) * 1000)
                        except ValueError:
                            retry_after_ms = None
                    # Drain body for connection reuse. Cap read to avoid OOM.
                    if status >= 400:
                        try:
                            await resp.content.read(2048)
                        except Exception:
                            pass
                    out.append(Outcome(t0, "amp", stage, mult, latency, status, err,
                                        retry_after_ms=retry_after_ms))
            except asyncio.TimeoutError:
                latency = (time.monotonic() - t0) * 1000
                out.append(Outcome(t0, "amp", stage, mult, latency, None, "Timeout"))
            except aiohttp.ClientError as e:
                latency = (time.monotonic() - t0) * 1000
                out.append(Outcome(t0, "amp", stage, mult, latency, None, type(e).__name__))


# ---------------------------------------------------------------------------
# Ramp driver
# ---------------------------------------------------------------------------

async def drive_stage(
    stage_idx: int,
    multiplier: float,
    duration_sec: int,
    xray_target_tps: float,
    amp_target_rps: float,
    xray_sender: XRaySender | None,
    amp_sender: AmpSender | None,
    outcomes: list[Outcome],
    kill_switch: Path | None,
) -> None:
    """
    Drives one stage. Each service gets a per-second token bucket: at the top
    of every wallclock second we schedule `target_tps` tasks, spaced evenly
    across the second, and let them fly. If the previous second's tasks
    haven't finished, they still count toward achieved throughput because
    outcomes are timestamped at dispatch.
    """
    start = time.monotonic()
    end = start + duration_sec
    tasks: list[asyncio.Task] = []

    # Integer target with fractional carry so we can schedule < 1 rps.
    xray_carry = 0.0
    amp_carry = 0.0
    tick = 0

    while time.monotonic() < end:
        if kill_switch and kill_switch.exists():
            print(f"kill-switch tripped at stage {stage_idx}, aborting", file=sys.stderr)
            break

        second_start = time.monotonic()

        # Fractional-rate token accounting: e.g. 0.5 rps → send every other tick.
        xray_carry += xray_target_tps
        amp_carry += amp_target_rps
        xray_this_tick = int(xray_carry)
        amp_this_tick = int(amp_carry)
        xray_carry -= xray_this_tick
        amp_carry -= amp_this_tick

        # Space dispatch evenly across the second.
        if xray_sender and xray_this_tick:
            gap = 1.0 / xray_this_tick
            for i in range(xray_this_tick):
                delay = i * gap
                tasks.append(asyncio.create_task(
                    _delayed(delay, xray_sender.send_one(stage_idx, multiplier, outcomes))
                ))
        if amp_sender and amp_this_tick:
            gap = 1.0 / amp_this_tick
            for i in range(amp_this_tick):
                delay = i * gap
                tasks.append(asyncio.create_task(
                    _delayed(delay, amp_sender.send_one(stage_idx, multiplier, outcomes))
                ))

        # Sleep until the next wallclock second boundary.
        elapsed = time.monotonic() - second_start
        if elapsed < 1.0:
            await asyncio.sleep(1.0 - elapsed)
        tick += 1

    # Wait for the tail of in-flight requests, capped so a stuck stage can't hang the run.
    if tasks:
        try:
            await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True),
                                    timeout=max(30.0, duration_sec * 0.5))
        except asyncio.TimeoutError:
            print(f"stage {stage_idx}: tail-drain timeout, {sum(1 for t in tasks if not t.done())} tasks still pending",
                  file=sys.stderr)


async def _delayed(delay: float, coro):
    if delay > 0:
        await asyncio.sleep(delay)
    return await coro


# ---------------------------------------------------------------------------
# Session bootstrap
# ---------------------------------------------------------------------------

def build_session(args) -> boto3.Session:
    if os.environ.get("AWS_WEB_IDENTITY_TOKEN_FILE") and os.environ.get("AWS_ROLE_ARN"):
        return boto3.Session(region_name=args.region)
    if args.assume_role_arn:
        base = boto3.Session(region_name=args.region)
        creds = base.client("sts").assume_role(
            RoleArn=args.assume_role_arn,
            RoleSessionName=f"otel-loadtest-{int(time.time())}",
            DurationSeconds=max(3600, args.stage_duration_sec * 11 + 300),
        )["Credentials"]
        return boto3.Session(
            aws_access_key_id=creds["AccessKeyId"],
            aws_secret_access_key=creds["SecretAccessKey"],
            aws_session_token=creds["SessionToken"],
            region_name=args.region,
        )
    if args.env.lower() in {"prod", "production"} and not args.allow_ambient:
        sys.stderr.write(
            "Refusing to load-test production with ambient credentials.\n"
            "Pass --assume-role-arn <role> or --allow-ambient explicitly.\n"
        )
        sys.exit(2)
    return boto3.Session(region_name=args.region)


# ---------------------------------------------------------------------------
# AWS-side correlation (CloudWatch metrics)
# ---------------------------------------------------------------------------

def fetch_aws_side_signals(
    session: boto3.Session, region: str,
    start: dt.datetime, end: dt.datetime,
    amp_workspace_id: str | None,
) -> dict[str, Any]:
    """
    Pulls the AWS-side counters that corroborate what the client saw:
    * AWS/Usage CallCount + ThrottleCount for xray & aps
    * AWS/Prometheus IngestionRate + DiscardedSamples (if workspace given)
    """
    cw = session.client("cloudwatch")
    out: dict[str, Any] = {"start": start.isoformat(), "end": end.isoformat()}

    def _get(namespace, metric, dims, stat="Sum", period=60):
        try:
            resp = cw.get_metric_statistics(
                Namespace=namespace, MetricName=metric, Dimensions=dims,
                StartTime=start, EndTime=end, Period=period,
                Statistics=[stat],
            )
            pts = sorted(resp.get("Datapoints", []), key=lambda d: d["Timestamp"])
            return [{"t": p["Timestamp"].isoformat(), "v": p.get(stat)} for p in pts]
        except ClientError as e:
            return {"error": e.response.get("Error", {}).get("Code", "Unknown")}

    out["xray_call_count"] = _get(
        "AWS/Usage", "CallCount",
        [{"Name": "Type", "Value": "API"},
         {"Name": "Resource", "Value": "PutTraceSegments"},
         {"Name": "Service", "Value": "X-Ray"},
         {"Name": "Class", "Value": "None"}],
    )
    out["xray_throttle_count"] = _get(
        "AWS/Usage", "ThrottleCount",
        [{"Name": "Type", "Value": "API"},
         {"Name": "Resource", "Value": "PutTraceSegments"},
         {"Name": "Service", "Value": "X-Ray"},
         {"Name": "Class", "Value": "None"}],
    )
    if amp_workspace_id:
        wdim = [{"Name": "WorkspaceId", "Value": amp_workspace_id}]
        out["amp_ingestion_rate"] = _get("AWS/Prometheus", "IngestionRate", wdim, "Average")
        out["amp_discarded_samples"] = _get(
            "AWS/Prometheus", "DiscardedSamples", wdim, "Sum",
        )
        out["amp_active_series"] = _get("AWS/Prometheus", "ActiveSeries", wdim, "Maximum")
    return out


# ---------------------------------------------------------------------------
# Report building
# ---------------------------------------------------------------------------

def _pct(vals: list[float], q: float) -> float:
    if not vals:
        return 0.0
    s = sorted(vals)
    k = (len(s) - 1) * q
    lo = math.floor(k); hi = math.ceil(k)
    if lo == hi:
        return s[lo]
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


def summarize_stage(stage: StageStats, outcomes: list[Outcome], stage_start: float,
                    stage_end: float) -> None:
    xray_lat: list[float] = []
    amp_lat: list[float] = []
    for o in outcomes:
        if o.stage != stage.stage:
            continue
        if o.service == "xray":
            stage.xray_sent += 1
            xray_lat.append(o.latency_ms)
            if o.http_status == 200 and not (o.error_code and o.error_code.startswith("Unprocessed")):
                stage.xray_ok += 1
            elif o.http_status == 403 or (o.error_code and "AccessDenied" in o.error_code):
                stage.xray_403 += 1
            elif o.http_status == 429 or o.error_code in {"ThrottledException", "Throttling", "TooManyRequestsException"}:
                stage.xray_429 += 1
            elif o.http_status and 500 <= o.http_status < 600:
                stage.xray_5xx += 1
            else:
                stage.xray_other_err += 1
        else:
            stage.amp_sent += 1
            amp_lat.append(o.latency_ms)
            if o.http_status and 200 <= o.http_status < 300:
                stage.amp_ok += 1
            elif o.http_status == 403:
                stage.amp_403 += 1
            elif o.http_status == 429:
                stage.amp_429 += 1
            elif o.http_status and 500 <= o.http_status < 600:
                stage.amp_5xx += 1
            else:
                stage.amp_other_err += 1

    duration = max(0.001, stage_end - stage_start)
    stage.xray_achieved_tps = stage.xray_sent / duration
    stage.amp_achieved_samples_per_sec = (stage.amp_sent * 1.0) / duration  # requests/sec — sample rate = req/sec * samples_per_req
    stage.xray_p50_ms = _pct(xray_lat, 0.50)
    stage.xray_p99_ms = _pct(xray_lat, 0.99)
    stage.amp_p50_ms = _pct(amp_lat, 0.50)
    stage.amp_p99_ms = _pct(amp_lat, 0.99)


def write_reports(
    ctx: dict[str, Any], stages: list[StageStats], outcomes: list[Outcome],
    aws_signals: dict[str, Any], out_dir: Path, samples_per_req: int,
) -> tuple[Path, Path, Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    base = out_dir / f"otel-loadtest-{ctx.get('env', 'unknown')}-{stamp}"

    # JSON
    payload = {
        "context": ctx,
        "stages": [asdict(s) for s in stages],
        "aws_signals": aws_signals,
        "verdict": _verdict(stages, ctx["error_budget_pct"]),
    }
    json_path = base.with_suffix(".json")
    json_path.write_text(json.dumps(payload, indent=2, default=str))

    # CSV (chartable)
    csv_path = base.with_suffix(".csv")
    with csv_path.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow([
            "stage", "multiplier",
            "xray_target_tps", "xray_achieved_tps", "xray_sent",
            "xray_ok", "xray_403", "xray_429", "xray_5xx", "xray_other",
            "xray_err_rate", "xray_p50_ms", "xray_p99_ms",
            "amp_target_rps", "amp_achieved_rps", "amp_sample_rate", "amp_sent",
            "amp_ok", "amp_403", "amp_429", "amp_5xx", "amp_other",
            "amp_err_rate", "amp_p50_ms", "amp_p99_ms",
        ])
        for s in stages:
            w.writerow([
                s.stage, s.multiplier,
                s.xray_target_tps, round(s.xray_achieved_tps, 2), s.xray_sent,
                s.xray_ok, s.xray_403, s.xray_429, s.xray_5xx, s.xray_other_err,
                round(s.xray_error_rate * 100, 2), round(s.xray_p50_ms, 1), round(s.xray_p99_ms, 1),
                round(s.amp_target_samples_per_sec / max(1, samples_per_req), 2),
                round(s.amp_achieved_samples_per_sec, 2),
                round(s.amp_achieved_samples_per_sec * samples_per_req, 2), s.amp_sent,
                s.amp_ok, s.amp_403, s.amp_429, s.amp_5xx, s.amp_other_err,
                round(s.amp_error_rate * 100, 2), round(s.amp_p50_ms, 1), round(s.amp_p99_ms, 1),
            ])

    # Markdown
    md_path = base.with_suffix(".md")
    md_path.write_text(render_markdown(ctx, stages, payload["verdict"], aws_signals, samples_per_req))

    # GitHub issue draft
    issue_path = out_dir / f"otel-loadtest-{ctx.get('env', 'unknown')}-{stamp}-github-issue.md"
    issue_path.write_text(render_github_issue(ctx, stages, aws_signals, samples_per_req))

    return json_path, csv_path, md_path, issue_path


def _verdict(stages: list[StageStats], budget_pct: float) -> dict[str, Any]:
    knee_x = None
    knee_a = None
    for s in stages:
        if knee_x is None and s.xray_error_rate * 100 > budget_pct:
            knee_x = s.multiplier
        if knee_a is None and s.amp_error_rate * 100 > budget_pct:
            knee_a = s.multiplier
    return {
        "error_budget_pct": budget_pct,
        "xray_knee_multiplier": knee_x,
        "amp_knee_multiplier": knee_a,
        "passed": knee_x is None and knee_a is None,
    }


def render_markdown(ctx, stages, verdict, aws_signals, samples_per_req) -> str:
    L = []
    env = ctx.get("env", "unknown")
    L.append(f"# OTel load-injection sensitivity report — {env}")
    L.append("")
    L.append(f"- Run started: `{ctx['started_at']}`  finished: `{ctx['finished_at']}`")
    L.append(f"- Region: `{ctx['region']}`  Account: `{ctx.get('account_id')}`")
    L.append(f"- Caller: `{ctx.get('caller_arn')}`")
    L.append(f"- Baseline: X-Ray **{ctx['baseline_xray_tps']} segments/s**, "
              f"AMP **{ctx['baseline_amp_samples_per_sec']} samples/s** "
              f"(= {ctx['baseline_amp_samples_per_sec']/samples_per_req:.1f} req/s × "
              f"{samples_per_req} samples/req)")
    L.append(f"- Error budget: **{ctx['error_budget_pct']}%** per stage")
    L.append(f"- Verdict: **{'PASS' if verdict['passed'] else 'FAIL'}** "
              f"— X-Ray knee at multiplier `{verdict['xray_knee_multiplier']}`, "
              f"AMP knee at `{verdict['amp_knee_multiplier']}`")
    L.append("")
    L.append("## Stage results")
    L.append("")
    L.append("| # | ×  | X-Ray target | X-Ray achieved | X-Ray err% | 429/403 | p99 ms | AMP target sps | AMP achieved sps | AMP err% | 429/403 | p99 ms |")
    L.append("|---|----|--------------|----------------|-----------|---------|--------|----------------|------------------|---------|---------|--------|")
    for s in stages:
        amp_ach_sps = s.amp_achieved_samples_per_sec * samples_per_req
        L.append(
            f"| {s.stage} | {s.multiplier}× | {s.xray_target_tps:.0f} | {s.xray_achieved_tps:.1f} | "
            f"{s.xray_error_rate*100:.1f}% | {s.xray_429}/{s.xray_403} | {s.xray_p99_ms:.0f} | "
            f"{s.amp_target_samples_per_sec:.0f} | {amp_ach_sps:.0f} | "
            f"{s.amp_error_rate*100:.1f}% | {s.amp_429}/{s.amp_403} | {s.amp_p99_ms:.0f} |"
        )
    L.append("")

    L.append("## AWS-side signals")
    L.append("")
    xray_throttle_sum = _sum_datapoints(aws_signals.get("xray_throttle_count"))
    xray_calls_sum = _sum_datapoints(aws_signals.get("xray_call_count"))
    L.append(f"- `AWS/Usage` X-Ray `CallCount` (sum): **{xray_calls_sum}**")
    L.append(f"- `AWS/Usage` X-Ray `ThrottleCount` (sum): **{xray_throttle_sum}**")
    if "amp_ingestion_rate" in aws_signals:
        peak_ing = _max_datapoints(aws_signals.get("amp_ingestion_rate"))
        disc = _sum_datapoints(aws_signals.get("amp_discarded_samples"))
        peak_series = _max_datapoints(aws_signals.get("amp_active_series"))
        L.append(f"- AMP peak `IngestionRate`: **{peak_ing}**")
        L.append(f"- AMP `DiscardedSamples` (sum): **{disc}**")
        L.append(f"- AMP peak `ActiveSeries`: **{peak_series}**")
    L.append("")

    L.append("## 403 / AccessDenied breakdown")
    L.append("")
    total_403 = sum(s.xray_403 + s.amp_403 for s in stages)
    if total_403 == 0:
        L.append("_No 403/AccessDenied responses observed at any stage — the role's permissions are sufficient for the load range tested._")
    else:
        L.append(f"**{total_403}** total 403 responses. Any 403 during a load ramp means the role lost permission "
                  "under contention (usually STS session expiry or a scoped-down data-plane condition). "
                  "This is orthogonal to throttling and should be triaged in `github_issue.md`.")
    L.append("")
    return "\n".join(L) + "\n"


def _sum_datapoints(dp) -> str:
    if not isinstance(dp, list):
        return "n/a"
    return f"{sum((p.get('v') or 0) for p in dp):.0f}"


def _max_datapoints(dp) -> str:
    if not isinstance(dp, list) or not dp:
        return "n/a"
    return f"{max((p.get('v') or 0) for p in dp):.1f}"


def render_github_issue(ctx, stages, aws_signals, samples_per_req) -> str:
    env = ctx.get("env", "unknown")
    xray_default_quota = 2600  # segments/sec/region, AWS default
    amp_default_ingest = 70_000  # samples/sec workspace default
    verdict = _verdict(stages, ctx["error_budget_pct"])

    # Recommend the smallest quota that covers 10x with a 40% headroom cushion.
    peak_xray = max(s.xray_target_tps for s in stages)
    peak_amp = max(s.amp_target_samples_per_sec for s in stages)
    rec_xray = int(math.ceil(peak_xray * 1.4 / 100.0) * 100)
    rec_amp = int(math.ceil(peak_amp * 1.4 / 10_000.0) * 10_000)

    xray_needs_bump = rec_xray > xray_default_quota or (verdict["xray_knee_multiplier"] is not None)
    amp_needs_bump = rec_amp > amp_default_ingest or (verdict["amp_knee_multiplier"] is not None)

    L = []
    L.append(f"# OTel pipeline: quota + backoff changes required to survive 10× peak ({env})")
    L.append("")
    L.append("## Summary")
    L.append("")
    L.append(f"- Ramp: 1× → 10× baseline ({ctx['baseline_xray_tps']} X-Ray seg/s, "
              f"{ctx['baseline_amp_samples_per_sec']} AMP samples/s)")
    if verdict["passed"]:
        L.append(f"- Pipeline stayed under the {ctx['error_budget_pct']}% error budget through 10×.")
    else:
        L.append(f"- **Error budget breached** at X-Ray {verdict['xray_knee_multiplier']}×, AMP {verdict['amp_knee_multiplier']}×.")
    L.append("")
    L.append("Attach the full report from the run:")
    L.append("- `otel-loadtest-*.md`")
    L.append("- `otel-loadtest-*.csv` (chartable)")
    L.append("- `otel-loadtest-*.json` (raw)")
    L.append("")

    # ---- X-Ray recommendation -------------------------------------------
    L.append("## AWS X-Ray")
    L.append("")
    L.append(f"- Default region quota: **{xray_default_quota} segments/sec** "
              "([service quotas](https://docs.aws.amazon.com/general/latest/gr/xray.html)).")
    L.append(f"- Observed peak target: **{peak_xray:.0f} seg/s** at 10×.")
    if xray_needs_bump:
        L.append(f"- **Requested quota:** raise `Segments per second` in `us-east-1` "
                  f"(quota code `L-xxxxxxxx`, service `xray`) to **{rec_xray}** "
                  "(≈ 40% headroom above 10× peak).")
        L.append("- File via Service Quotas console → *AWS X-Ray* → *Segments per second* → *Request quota increase*.")
    else:
        L.append("- No quota bump needed at the tested peak.")
    L.append("")
    L.append("**Sender-side changes (aws-otel-collector / adot-collector):**")
    L.append("")
    L.append("```yaml")
    L.append("# collector config: exporters.awsxray")
    L.append("exporters:")
    L.append("  awsxray:")
    L.append("    # AWS SDK retry: exponential + jitter, honor ThrottledException")
    L.append("    resource_arn: \"\"")
    L.append("    # collector-level batching keeps per-call segment count high so")
    L.append("    # a single PutTraceSegments carries more work per token.")
    L.append("  # processors.batch:")
    L.append("  #   send_batch_size: 50")
    L.append("  #   send_batch_max_size: 50")
    L.append("  #   timeout: 200ms")
    L.append("```")
    L.append("")
    L.append("Also set on the AWS SDK / collector:")
    L.append("- `AWS_MAX_ATTEMPTS=8`, `AWS_RETRY_MODE=adaptive` "
              "(adaptive mode adds client-side throttling once ThrottledException is observed).")
    L.append("- Drop batch to `send_batch_size=50` (X-Ray segments are 64 KB max — larger batches raise 4xx risk).")
    L.append("")

    # ---- AMP recommendation ---------------------------------------------
    L.append("## Amazon Managed Service for Prometheus")
    L.append("")
    L.append(f"- Default workspace quota: **{amp_default_ingest:,} samples/sec ingestion** "
              "([AMP quotas](https://docs.aws.amazon.com/prometheus/latest/userguide/AMP_quotas.html)).")
    L.append(f"- Observed peak target: **{peak_amp:.0f} samples/s** at 10×.")
    if amp_needs_bump:
        L.append(f"- **Requested quota:** raise `Ingestion rate per workspace` on workspace "
                  "`<workspace-id>` to **{:,}** samples/s.".format(rec_amp))
        L.append("- AMP uses a token bucket; docs also warn about throttling if you "
                  "*double* your prior 30-min baseline. Ramp callers gradually or "
                  "coordinate a burst window with AWS support before cutover.")
    else:
        L.append("- No quota bump needed at the tested peak.")
    L.append("")
    L.append("**Sender-side changes (Prometheus / ADOT remote_write):**")
    L.append("")
    L.append("```yaml")
    L.append("remote_write:")
    L.append("  - url: https://aps-workspaces.<region>.amazonaws.com/workspaces/<ws>/api/v1/remote_write")
    L.append("    sigv4: { region: <region> }")
    L.append("    queue_config:")
    L.append("      capacity: 10000        # samples buffered in-memory per shard")
    L.append("      max_shards: 200        # scale-out on sustained backlog")
    L.append("      min_shards: 4          # keep enough parallelism for cold starts")
    L.append("      max_samples_per_send: 2000")
    L.append("      batch_send_deadline: 5s")
    L.append("      min_backoff: 500ms     # honour AMP token-bucket refill")
    L.append("      max_backoff: 30s")
    L.append("      retry_on_http_429: true  # default in modern Prometheus; keep on")
    L.append("```")
    L.append("")
    L.append("- If the collector is ADOT/OTel `prometheusremotewrite`, set "
              "`retry_on_failure.enabled: true` with `initial_interval: 1s`, "
              "`max_interval: 30s`, `max_elapsed_time: 5m`, plus `sending_queue.queue_size` "
              "≥ 60 s of expected 10× samples.")
    L.append("- Add `write_relabel_configs` to drop high-cardinality Go runtime / "
              "container-scoped labels before they hit AMP; the AMP knee is nearly "
              "always active series, not raw sample rate.")
    L.append("")

    L.append("## 403 / AccessDenied observations")
    L.append("")
    total_403 = sum(s.xray_403 + s.amp_403 for s in stages)
    if total_403 == 0:
        L.append("None observed. Skip the IAM section of the ticket.")
    else:
        L.append(f"{total_403} intermittent 403s observed during ramp. Likely causes, in order:")
        L.append("1. STS session expired mid-ramp — re-assume the role every ≤ 45 min or shorten ramp.")
        L.append("2. `aps:RemoteWrite` scoped to a specific workspace ARN that no longer matches (e.g. new workspace created).")
        L.append("3. SigV4 clock skew on the sender — enforce NTP.")
        L.append("")
        L.append("Action: pull the 403 rows from the CSV, cross-check `sts:AssumeRoleWithWebIdentity` "
                  "timestamps in CloudTrail, confirm the role ARN in the collector matches the module output.")
    L.append("")

    L.append("## Acceptance criteria")
    L.append("")
    L.append(f"- [ ] Quota increases granted for X-Ray and/or AMP as listed above.")
    L.append(f"- [ ] Collector config PR merged with the queue_config + retry values above.")
    L.append(f"- [ ] Re-run `otel_loadtest.py` at 10× and confirm error rate < {ctx['error_budget_pct']}% and zero 403s.")
    L.append(f"- [ ] Alarm added on `AWS/Usage` `ThrottleCount` for X-Ray and `AWS/Prometheus` `DiscardedSamples` for AMP.")
    L.append("")
    L.append("## References")
    L.append("- [AWS X-Ray service quotas](https://docs.aws.amazon.com/general/latest/gr/xray.html)")
    L.append("- [X-Ray PutTraceSegments API — ThrottledException](https://docs.aws.amazon.com/it_it/xray/latest/api/API_PutTraceSegments.html)")
    L.append("- [AMP service quotas](https://docs.aws.amazon.com/prometheus/latest/userguide/AMP_quotas.html)")
    L.append("- [AMP troubleshooting: 429 errors](https://docs.aws.amazon.com/prometheus/latest/userguide/AMP-troubleshooting.html)")
    return "\n".join(L) + "\n"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="OTel pipeline load injector (1x → 10x).")
    p.add_argument("--region", required=True)
    p.add_argument("--env", default="staging")
    p.add_argument("--baseline-xray-tps", type=float, required=True,
                   help="Current peak X-Ray PutTraceSegments TPS (calls/sec).")
    p.add_argument("--baseline-amp-samples-per-sec", type=float, required=True,
                   help="Current peak AMP samples/sec (samples/sec, not requests/sec).")
    p.add_argument("--xray-batch-size", type=int, default=50,
                   help="Segments per PutTraceSegments call. 50 is the AWS-recommended cap.")
    p.add_argument("--amp-samples-per-request", type=int, default=1000,
                   help="Samples per remote_write POST. Divides target samples/s to get request rate.")
    p.add_argument("--amp-endpoint", default="",
                   help="Full AMP remote_write URL. Empty = skip AMP.")
    p.add_argument("--amp-workspace-id", default="",
                   help="Workspace ID for AMP CloudWatch metric correlation (optional).")
    p.add_argument("--stage-duration-sec", type=int, default=60)
    p.add_argument("--stage-cooldown-sec", type=int, default=15)
    p.add_argument("--max-multiplier", type=int, default=10)
    p.add_argument("--min-multiplier", type=int, default=1)
    p.add_argument("--xray-concurrency", type=int, default=64)
    p.add_argument("--amp-concurrency", type=int, default=64)
    p.add_argument("--error-budget-pct", type=float, default=1.0,
                   help="Per-stage error-rate ceiling. Ramp fails if any stage exceeds it.")
    p.add_argument("--max-total-requests", type=int, default=2_000_000,
                   help="Hard cap on total sent requests (safety rail).")
    p.add_argument("--max-wallclock-sec", type=int, default=45 * 60,
                   help="Hard wallclock cap for the whole run (safety rail).")
    p.add_argument("--kill-switch-file", default="",
                   help="If this file exists mid-run, the ramp aborts cleanly.")
    p.add_argument("--assume-role-arn", default="")
    p.add_argument("--allow-ambient", action="store_true")
    p.add_argument("--report-dir", default="./reports")
    p.add_argument("--dry-run", action="store_true",
                   help="Print the ramp plan and exit without sending traffic.")
    return p.parse_args()


async def _main_async(args) -> int:
    ctx: dict[str, Any] = {
        "started_at": dt.datetime.utcnow().isoformat(timespec="seconds") + "Z",
        "region": args.region,
        "env": args.env,
        "host": socket.gethostname(),
        "user": getpass.getuser(),
        "python": platform.python_version(),
        "baseline_xray_tps": args.baseline_xray_tps,
        "baseline_amp_samples_per_sec": args.baseline_amp_samples_per_sec,
        "amp_samples_per_request": args.amp_samples_per_request,
        "xray_batch_size": args.xray_batch_size,
        "stage_duration_sec": args.stage_duration_sec,
        "stage_cooldown_sec": args.stage_cooldown_sec,
        "error_budget_pct": args.error_budget_pct,
    }

    plan = []
    for mult in range(args.min_multiplier, args.max_multiplier + 1):
        plan.append({
            "stage": mult,
            "multiplier": float(mult),
            "xray_target_tps": args.baseline_xray_tps * mult,
            "amp_target_samples_per_sec": args.baseline_amp_samples_per_sec * mult,
            "amp_target_rps": (args.baseline_amp_samples_per_sec * mult) / args.amp_samples_per_request,
        })
    ctx["plan"] = plan

    if args.dry_run:
        print(json.dumps(ctx, indent=2, default=str))
        return 0

    session = build_session(args)
    try:
        who = session.client("sts").get_caller_identity()
        ctx["account_id"] = who["Account"]
        ctx["caller_arn"] = who["Arn"]
    except Exception as e:
        sys.stderr.write(f"sts:GetCallerIdentity failed: {e}\n")
        return 2

    ks = Path(args.kill_switch_file) if args.kill_switch_file else None
    xray_sender = XRaySender(session, args.xray_batch_size, args.xray_concurrency)
    amp_sender = AmpSender(session, args.amp_endpoint, args.region,
                            args.amp_samples_per_request, args.amp_concurrency) \
        if args.amp_endpoint else None

    outcomes: list[Outcome] = []
    stages: list[StageStats] = []
    run_start = time.monotonic()
    aborted = False

    for stage_def in plan:
        if time.monotonic() - run_start > args.max_wallclock_sec:
            print("wallclock cap reached, aborting ramp", file=sys.stderr)
            aborted = True
            break
        if len(outcomes) > args.max_total_requests:
            print("request cap reached, aborting ramp", file=sys.stderr)
            aborted = True
            break

        stage = StageStats(
            stage=stage_def["stage"], multiplier=stage_def["multiplier"],
            xray_target_tps=stage_def["xray_target_tps"],
            amp_target_samples_per_sec=stage_def["amp_target_samples_per_sec"],
            started_at=dt.datetime.utcnow().isoformat(timespec="seconds") + "Z",
            finished_at="",
        )
        stage_start = time.monotonic()
        print(f"[stage {stage.stage}] {stage.multiplier}× → "
              f"X-Ray {stage.xray_target_tps:.0f} tps, "
              f"AMP {stage_def['amp_target_rps']:.1f} req/s "
              f"({stage.amp_target_samples_per_sec:.0f} samples/s)")

        await drive_stage(
            stage_idx=stage.stage, multiplier=stage.multiplier,
            duration_sec=args.stage_duration_sec,
            xray_target_tps=stage.xray_target_tps,
            amp_target_rps=stage_def["amp_target_rps"] if amp_sender else 0.0,
            xray_sender=xray_sender, amp_sender=amp_sender,
            outcomes=outcomes, kill_switch=ks,
        )
        stage_end = time.monotonic()
        stage.finished_at = dt.datetime.utcnow().isoformat(timespec="seconds") + "Z"
        summarize_stage(stage, outcomes, stage_start, stage_end)
        stages.append(stage)
        print(f"  → sent x={stage.xray_sent} a={stage.amp_sent}  "
              f"err x={stage.xray_error_rate*100:.1f}% a={stage.amp_error_rate*100:.1f}%  "
              f"429 x={stage.xray_429} a={stage.amp_429}  403 x={stage.xray_403} a={stage.amp_403}")

        if ks and ks.exists():
            aborted = True
            break

        if stage.stage != args.max_multiplier:
            await asyncio.sleep(args.stage_cooldown_sec)

    if amp_sender:
        await amp_sender.close()

    ctx["finished_at"] = dt.datetime.utcnow().isoformat(timespec="seconds") + "Z"
    ctx["aborted"] = aborted

    # AWS-side correlation
    run_end_dt = dt.datetime.utcnow()
    run_start_dt = run_end_dt - dt.timedelta(seconds=int(time.monotonic() - run_start) + 60)
    aws_signals = fetch_aws_side_signals(
        session, args.region, run_start_dt, run_end_dt + dt.timedelta(minutes=2),
        args.amp_workspace_id or None,
    )

    json_p, csv_p, md_p, issue_p = write_reports(
        ctx, stages, outcomes, aws_signals, Path(args.report_dir),
        args.amp_samples_per_request,
    )
    print(f"\nReports written:\n  {md_p}\n  {csv_p}\n  {json_p}\n  {issue_p}")

    verdict = _verdict(stages, args.error_budget_pct)
    if aborted:
        return 2
    return 0 if verdict["passed"] else 1


def main() -> int:
    args = parse_args()
    return asyncio.run(_main_async(args))


if __name__ == "__main__":
    sys.exit(main())
