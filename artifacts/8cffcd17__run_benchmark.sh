#!/usr/bin/env bash
# run_benchmark.sh — execute a 5-minute trace load test against
# ./dist/otelcol-custom and emit a benchmarks/reports/latest.json summary.
#
# Metrics captured:
#   * Ingest throughput: spans/sec accepted by the OTLP receiver
#     (delta of otelcol_receiver_accepted_spans over the run).
#   * Export latency: p50/p95/p99 in milliseconds, derived from
#     otelcol_exporter_send_latency_bucket histogram.
#   * Process CPU: mean and peak % of a single core, from pidstat samples.
#   * Process RSS: mean and peak resident set in bytes, from pidstat samples.
#
# Everything writes to $REPORTS_DIR; nothing outside the repo is touched.
#
# Env knobs (all optional):
#   BENCH_DURATION_SEC  default 300  (5 minutes)
#   BENCH_RATE          default 1000 spans/sec per worker
#   BENCH_WORKERS       default 2
#   BENCH_BINARY        default ./dist/otelcol-custom
#   BENCH_CONFIG        default benchmarks/config/config.bench.yaml
#   REPORTS_DIR         default benchmarks/reports
#   HISTORY_DIR         default benchmarks/history
#   SAMPLE_INTERVAL_SEC default 1

set -euo pipefail

BENCH_DURATION_SEC="${BENCH_DURATION_SEC:-300}"
BENCH_RATE="${BENCH_RATE:-1000}"
BENCH_WORKERS="${BENCH_WORKERS:-2}"
BENCH_BINARY="${BENCH_BINARY:-./dist/otelcol-custom}"
BENCH_CONFIG="${BENCH_CONFIG:-benchmarks/config/config.bench.yaml}"
REPORTS_DIR="${REPORTS_DIR:-benchmarks/reports}"
HISTORY_DIR="${HISTORY_DIR:-benchmarks/history}"
SAMPLE_INTERVAL_SEC="${SAMPLE_INTERVAL_SEC:-1}"

METRICS_URL="http://127.0.0.1:8888/metrics"
OTLP_ENDPOINT="127.0.0.1:4317"

mkdir -p "$REPORTS_DIR" "$HISTORY_DIR"
WORKDIR="$(mktemp -d)"
trap 'rm -rf "$WORKDIR"' EXIT

log() { printf '[%s] %s\n' "$(date -u +%H:%M:%S)" "$*"; }

# ---------------------------------------------------------------------------
# Sanity checks
# ---------------------------------------------------------------------------
[ -x "$BENCH_BINARY" ] || { echo "::error::binary not found: $BENCH_BINARY"; exit 1; }
[ -f "$BENCH_CONFIG" ] || { echo "::error::config not found: $BENCH_CONFIG"; exit 1; }
command -v telemetrygen >/dev/null || { echo "::error::telemetrygen not on PATH"; exit 1; }
command -v pidstat      >/dev/null || { echo "::error::pidstat not on PATH (install sysstat)"; exit 1; }
command -v jq           >/dev/null || { echo "::error::jq not on PATH"; exit 1; }

# ---------------------------------------------------------------------------
# Start Collector
# ---------------------------------------------------------------------------
log "starting Collector: $BENCH_BINARY --config $BENCH_CONFIG"
"$BENCH_BINARY" --config "$BENCH_CONFIG" > "$WORKDIR/collector.log" 2>&1 &
COLLECTOR_PID=$!
log "Collector PID=$COLLECTOR_PID"

cleanup() {
  if kill -0 "$COLLECTOR_PID" 2>/dev/null; then
    kill -TERM "$COLLECTOR_PID" 2>/dev/null || true
    for _ in $(seq 1 10); do
      kill -0 "$COLLECTOR_PID" 2>/dev/null || break
      sleep 1
    done
    kill -0 "$COLLECTOR_PID" 2>/dev/null && kill -KILL "$COLLECTOR_PID" || true
  fi
  # Preserve collector log for debugging.
  cp "$WORKDIR/collector.log" "$REPORTS_DIR/collector.log" 2>/dev/null || true
}
trap 'cleanup; rm -rf "$WORKDIR"' EXIT

# Wait for OTLP + metrics endpoints.
for _ in $(seq 1 30); do
  if (echo > /dev/tcp/127.0.0.1/4317) 2>/dev/null && curl -sf "$METRICS_URL" >/dev/null; then
    log "Collector is ready"
    break
  fi
  sleep 1
done
kill -0 "$COLLECTOR_PID" 2>/dev/null || { echo "::error::Collector died during startup"; tail -n 40 "$WORKDIR/collector.log"; exit 1; }

# ---------------------------------------------------------------------------
# Snapshot receiver counter BEFORE the load starts.
# ---------------------------------------------------------------------------
scrape_counter() {
  # Sum all label combinations of a given counter metric.
  curl -sf "$METRICS_URL" \
    | awk -v m="$1" '$1 ~ ("^"m"(\\{|$)") { sum += $2 } END { printf "%.0f\n", sum+0 }'
}

BEFORE_ACCEPTED=$(scrape_counter otelcol_receiver_accepted_spans)
BEFORE_REFUSED=$(scrape_counter otelcol_receiver_refused_spans)
log "receiver counters at t0: accepted=$BEFORE_ACCEPTED refused=$BEFORE_REFUSED"

# ---------------------------------------------------------------------------
# Start pidstat sampler in background, then telemetrygen (foreground).
# ---------------------------------------------------------------------------
log "sampling process metrics every ${SAMPLE_INTERVAL_SEC}s"
# `-h -r -u -p PID N` -> one line per sample, header included, CPU + RSS in KiB.
pidstat -h -r -u -p "$COLLECTOR_PID" "$SAMPLE_INTERVAL_SEC" \
  > "$WORKDIR/pidstat.txt" 2>/dev/null &
PIDSTAT_PID=$!

RUN_START_EPOCH=$(date +%s)
log "running telemetrygen for ${BENCH_DURATION_SEC}s (rate=${BENCH_RATE}/s workers=${BENCH_WORKERS})"
telemetrygen traces \
  --otlp-endpoint "$OTLP_ENDPOINT" \
  --otlp-insecure \
  --duration "${BENCH_DURATION_SEC}s" \
  --rate "$BENCH_RATE" \
  --workers "$BENCH_WORKERS" \
  --service otelcol-bench \
  > "$WORKDIR/telemetrygen.log" 2>&1
RUN_END_EPOCH=$(date +%s)
ELAPSED_SEC=$((RUN_END_EPOCH - RUN_START_EPOCH))
log "telemetrygen finished in ${ELAPSED_SEC}s"

# Give the export queue a few seconds to drain before final scrape.
sleep 5

kill -TERM "$PIDSTAT_PID" 2>/dev/null || true
wait "$PIDSTAT_PID" 2>/dev/null || true

# ---------------------------------------------------------------------------
# Snapshot receiver counter + export-latency histogram AFTER the load.
# ---------------------------------------------------------------------------
AFTER_ACCEPTED=$(scrape_counter otelcol_receiver_accepted_spans)
AFTER_REFUSED=$(scrape_counter otelcol_receiver_refused_spans)
log "receiver counters at t1: accepted=$AFTER_ACCEPTED refused=$AFTER_REFUSED"

curl -sf "$METRICS_URL" > "$WORKDIR/metrics.after.txt"

DELTA_ACCEPTED=$((AFTER_ACCEPTED - BEFORE_ACCEPTED))
DELTA_REFUSED=$((AFTER_REFUSED - BEFORE_REFUSED))
THROUGHPUT_SPS=$(awk -v s="$DELTA_ACCEPTED" -v t="$ELAPSED_SEC" \
  'BEGIN{ if (t>0) printf "%.2f", s/t; else print 0 }')

# ---------------------------------------------------------------------------
# Compute latency percentiles from the exporter send-latency histogram.
# The metric is a cumulative Prometheus histogram with `le` buckets in seconds.
# We aggregate across all label combinations.
# ---------------------------------------------------------------------------
python3 - "$WORKDIR/metrics.after.txt" > "$WORKDIR/latency.json" <<'PY'
import sys, json, re
from collections import defaultdict

path = sys.argv[1]
buckets = defaultdict(float)   # le -> cumulative count
total_count = 0.0
total_sum   = 0.0

# Prefer the newer OTel semantic conventions metric name if present, else fall
# back to the classic one.
candidates = [
    "otelcol_exporter_send_latency",
    "otelcol_exporter_sent_latency",  # historical variant
]

def parse_le(labels):
    m = re.search(r'le="([^"]+)"', labels)
    return m.group(1) if m else None

hist_name = None
with open(path) as f:
    for line in f:
        if line.startswith("#") or not line.strip():
            continue
        # metric{labels} value
        m = re.match(r'([a-zA-Z0-9_:]+)(?:\{([^}]*)\})?\s+([0-9eE.+\-]+)', line)
        if not m:
            continue
        name, labels, value = m.group(1), m.group(2) or "", float(m.group(3))
        for base in candidates:
            if name == base + "_bucket":
                hist_name = base
                le = parse_le(labels)
                if le is not None:
                    buckets[le] += value
            elif name == base + "_count":
                hist_name = base
                total_count += value
            elif name == base + "_sum":
                hist_name = base
                total_sum += value

result = {"histogram": hist_name, "count": total_count, "sum_seconds": total_sum}

if buckets and total_count > 0:
    # Sort buckets by numeric le (+Inf last).
    def le_key(x):
        return float("inf") if x == "+Inf" else float(x)
    ordered = sorted(buckets.items(), key=lambda kv: le_key(kv[0]))

    def percentile(p):
        target = total_count * p
        prev_le, prev_cum = 0.0, 0.0
        for le, cum in ordered:
            le_val = le_key(le)
            if cum >= target:
                # Linear interp within the bucket.
                if le_val == float("inf"):
                    return prev_le * 1000.0  # ms, cap at previous bound
                if cum == prev_cum:
                    return le_val * 1000.0
                frac = (target - prev_cum) / (cum - prev_cum)
                return (prev_le + frac * (le_val - prev_le)) * 1000.0
            prev_le, prev_cum = le_val, cum
        return ordered[-1][0] and float("nan")

    result["p50_ms"] = percentile(0.50)
    result["p95_ms"] = percentile(0.95)
    result["p99_ms"] = percentile(0.99)
    result["mean_ms"] = (total_sum / total_count) * 1000.0 if total_count else None
else:
    result["p50_ms"] = None
    result["p95_ms"] = None
    result["p99_ms"] = None
    result["mean_ms"] = None

print(json.dumps(result))
PY

LAT_JSON="$(cat "$WORKDIR/latency.json")"

# ---------------------------------------------------------------------------
# Aggregate pidstat samples: mean/peak CPU%, mean/peak RSS (bytes).
# pidstat -u -r columns:
#   Time  UID  PID  %usr  %system  %guest  %wait  %CPU  CPU  minflt/s  majflt/s  VSZ  RSS  %MEM  Command
# Header line begins with '#'.
# ---------------------------------------------------------------------------
python3 - "$WORKDIR/pidstat.txt" > "$WORKDIR/proc.json" <<'PY'
import sys, json, statistics

path = sys.argv[1]
cpu_samples = []
rss_samples = []

with open(path) as f:
    header = None
    for line in f:
        line = line.rstrip()
        if not line:
            continue
        if line.startswith("#"):
            # e.g. "# Time UID PID %usr %system ... %CPU CPU minflt/s ... RSS ..."
            header = line.lstrip("#").split()
            continue
        if header is None:
            continue
        parts = line.split()
        if len(parts) < len(header):
            continue
        try:
            row = dict(zip(header, parts))
            cpu = float(row.get("%CPU", "nan"))
            rss_kib = float(row.get("RSS", "nan"))
            cpu_samples.append(cpu)
            rss_samples.append(rss_kib * 1024.0)  # -> bytes
        except ValueError:
            continue

def stats(xs):
    if not xs:
        return {"mean": None, "peak": None, "samples": 0}
    return {
        "mean": statistics.fmean(xs),
        "peak": max(xs),
        "samples": len(xs),
    }

print(json.dumps({"cpu_pct": stats(cpu_samples), "rss_bytes": stats(rss_samples)}))
PY

PROC_JSON="$(cat "$WORKDIR/proc.json")"

# ---------------------------------------------------------------------------
# Emit the run report.
# ---------------------------------------------------------------------------
GIT_SHA="${GITHUB_SHA:-$(git rev-parse HEAD 2>/dev/null || echo unknown)}"
RUN_ID="${GITHUB_RUN_ID:-local-$(date +%s)}"
RUN_URL="${GITHUB_SERVER_URL:-}/${GITHUB_REPOSITORY:-}/actions/runs/${GITHUB_RUN_ID:-}"
[ "$RUN_URL" = "/-/actions/runs/" ] && RUN_URL=""

REPORT="$REPORTS_DIR/latest.json"
jq -n \
  --arg   sha            "$GIT_SHA" \
  --arg   run_id         "$RUN_ID" \
  --arg   run_url        "$RUN_URL" \
  --arg   recorded_at    "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  --argjson duration_sec "$ELAPSED_SEC" \
  --argjson accepted     "$DELTA_ACCEPTED" \
  --argjson refused      "$DELTA_REFUSED" \
  --argjson throughput   "$THROUGHPUT_SPS" \
  --argjson latency      "$LAT_JSON" \
  --argjson proc         "$PROC_JSON" \
  --argjson knobs        "{\"duration_sec\":${BENCH_DURATION_SEC},\"rate\":${BENCH_RATE},\"workers\":${BENCH_WORKERS}}" \
  '{
     sha: $sha,
     run_id: $run_id,
     run_url: $run_url,
     recorded_at: $recorded_at,
     knobs: $knobs,
     duration_sec: $duration_sec,
     spans_accepted: $accepted,
     spans_refused:  $refused,
     throughput_sps: $throughput,
     latency: $latency,
     process: $proc
   }' > "$REPORT"

log "wrote $REPORT"
cat "$REPORT"

# Also append this run to the history log for the rolling baseline.
DATE_UTC="$(date -u +%Y-%m-%d)"
HISTORY_FILE="$HISTORY_DIR/${DATE_UTC}.json"
cp "$REPORT" "$HISTORY_FILE"
log "wrote $HISTORY_FILE"
