#!/usr/bin/env bash
# ------------------------------------------------------------------------------
# verify_rds_migration_config.sh
#
# Verifies that an Amazon RDS for PostgreSQL instance has the recommended
# "load window" settings from the migration tuning runbook applied before
# a bulk-load / migration begins.
#
# Checks:
#   - checkpoint_timeout
#   - max_wal_size
#   - maintenance_work_mem
#   - autovacuum_vacuum_scale_factor
#
# Also surfaces a handful of high-value companion settings so the operator can
# eyeball the wider WAL / autovacuum picture in the same run.
#
# Output: color-coded status report. Discrepancies from the target values are
# printed in RED. Values that match (or exceed, where "more is better") are
# printed in GREEN. Informational rows are printed in YELLOW.
#
# Usage:
#   ./verify_rds_migration_config.sh \
#       --host mydb.xxxxx.us-east-1.rds.amazonaws.com \
#       --port 5432 \
#       --user dbadmin \
#       --db   postgres
#
# Password is read from the PGPASSWORD environment variable, a ~/.pgpass file,
# or interactively (whatever psql itself would use). No secrets are logged.
#
# Exit code:
#   0  all critical settings match the runbook
#   1  one or more critical settings do NOT match the runbook
#   2  script / connection error
# ------------------------------------------------------------------------------

set -u
set -o pipefail

# -------------------------- defaults & CLI parsing ----------------------------

PGHOST="${PGHOST:-}"
PGPORT="${PGPORT:-5432}"
PGUSER="${PGUSER:-postgres}"
PGDATABASE="${PGDATABASE:-postgres}"
NO_COLOR=0

usage() {
  cat <<EOF
Usage: $(basename "$0") [--host H] [--port P] [--user U] [--db D] [--no-color]

Verifies RDS PostgreSQL configuration against the migration runbook.

Options:
  --host       RDS endpoint (or set PGHOST)
  --port       Port (default: 5432, or PGPORT)
  --user       DB user (default: postgres, or PGUSER)
  --db         Database name (default: postgres, or PGDATABASE)
  --no-color   Disable ANSI colors (useful for logs / CI)
  -h, --help   Show this help

Password: set PGPASSWORD, use ~/.pgpass, or enter interactively.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --host)     PGHOST="$2"; shift 2 ;;
    --port)     PGPORT="$2"; shift 2 ;;
    --user)     PGUSER="$2"; shift 2 ;;
    --db)       PGDATABASE="$2"; shift 2 ;;
    --no-color) NO_COLOR=1; shift ;;
    -h|--help)  usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage; exit 2 ;;
  esac
done

if [[ -z "$PGHOST" ]]; then
  echo "ERROR: --host (or PGHOST) is required." >&2
  usage
  exit 2
fi

if ! command -v psql >/dev/null 2>&1; then
  echo "ERROR: psql not found in PATH. Install the PostgreSQL client." >&2
  exit 2
fi

export PGHOST PGPORT PGUSER PGDATABASE

# --------------------------------- colors -------------------------------------

if [[ $NO_COLOR -eq 1 || ! -t 1 ]]; then
  RED=""; GREEN=""; YELLOW=""; CYAN=""; BOLD=""; DIM=""; RESET=""
else
  RED="$(printf '\033[31m')"
  GREEN="$(printf '\033[32m')"
  YELLOW="$(printf '\033[33m')"
  CYAN="$(printf '\033[36m')"
  BOLD="$(printf '\033[1m')"
  DIM="$(printf '\033[2m')"
  RESET="$(printf '\033[0m')"
fi

# ------------------------- connection sanity check ----------------------------

echo
echo "${BOLD}${CYAN}RDS PostgreSQL Migration Config Verifier${RESET}"
echo "${DIM}Endpoint: ${PGUSER}@${PGHOST}:${PGPORT}/${PGDATABASE}${RESET}"
echo

if ! psql -Xqt -v ON_ERROR_STOP=1 -c "SELECT 1;" >/dev/null 2>&1; then
  echo "${RED}ERROR:${RESET} Cannot connect to ${PGHOST}:${PGPORT} as ${PGUSER}." >&2
  echo "Check network reachability, security groups, credentials, and TLS." >&2
  exit 2
fi

PG_VERSION="$(psql -XAqt -c "SHOW server_version;" 2>/dev/null | tr -d '[:space:]')"
INSTANCE_RAM_MB="$(psql -XAqt -c "
  SELECT COALESCE(
    (SELECT setting::bigint * (SELECT setting::bigint FROM pg_settings WHERE name='block_size') / 1024 / 1024
       FROM pg_settings WHERE name='shared_buffers')
   , 0);" 2>/dev/null | tr -d '[:space:]')"

echo "${DIM}Server version: ${PG_VERSION}${RESET}"
echo

# --------------------- helpers: fetch settings as bytes/ms --------------------

# Returns the setting in its base unit as bytes (memory) or milliseconds (time).
# Uses pg_settings.unit + setting.
fetch_setting_normalized() {
  local name="$1"
  psql -XAqt -F '|' -c "
    SELECT setting, unit
      FROM pg_settings
     WHERE name = '${name}';" 2>/dev/null
}

# Returns the raw display value (e.g. '30min', '32GB', '0.05')
fetch_setting_raw() {
  local name="$1"
  psql -XAqt -c "SHOW ${name};" 2>/dev/null | tr -d '[:space:]'
}

# Convert pg_settings (setting, unit) to a canonical numeric.
# - Memory units (kB, MB, 8kB, 16kB, ...): return BYTES.
# - Time units (ms, s, min, h, d): return MILLISECONDS.
# - No unit: return the raw number (float or int).
to_canonical() {
  local raw="$1"      # "setting|unit"
  local setting unit
  setting="${raw%%|*}"
  unit="${raw##*|}"
  if [[ -z "$setting" ]]; then
    echo ""
    return
  fi
  # unit may be empty for dimensionless (e.g., scale factors)
  awk -v s="$setting" -v u="$unit" 'BEGIN {
    # memory: block-based units are like "8kB", "16kB"
    if (u == "" ) { printf "%s", s; exit }
    if (u == "kB") { printf "%.0f", s * 1024; exit }
    if (u == "MB") { printf "%.0f", s * 1024 * 1024; exit }
    if (u == "GB") { printf "%.0f", s * 1024 * 1024 * 1024; exit }
    if (u ~ /^[0-9]+kB$/) {
      block = u; sub(/kB$/, "", block);
      printf "%.0f", s * block * 1024; exit
    }
    if (u == "ms")  { printf "%.0f", s; exit }
    if (u == "s")   { printf "%.0f", s * 1000; exit }
    if (u == "min") { printf "%.0f", s * 60 * 1000; exit }
    if (u == "h")   { printf "%.0f", s * 3600 * 1000; exit }
    if (u == "d")   { printf "%.0f", s * 86400 * 1000; exit }
    # unknown unit: fall back to raw
    printf "%s", s;
  }'
}

# Human-readable formatter
human_bytes() {
  awk -v b="$1" 'BEGIN {
    if (b == "" || b+0 != b) { print b; exit }
    split("B KB MB GB TB", u, " ");
    i = 1;
    while (b >= 1024 && i < 5) { b /= 1024; i++ }
    if (b == int(b)) printf "%d %s", b, u[i]; else printf "%.2f %s", b, u[i];
  }'
}

human_ms() {
  awk -v m="$1" 'BEGIN {
    if (m == "" || m+0 != m) { print m; exit }
    if (m < 1000) { printf "%d ms", m; exit }
    s = m / 1000;
    if (s < 60) { printf "%g s", s; exit }
    mn = s / 60;
    if (mn < 60) { printf "%g min", mn; exit }
    h = mn / 60;
    printf "%g h", h;
  }'
}

# ---------------------- target values from the runbook ------------------------
#
# See §3 of the runbook. Values below are the *minimum acceptable* settings
# for the load window. "More is better" for time/size targets. Autovacuum
# scale factor is "less is better" (more aggressive vacuuming).

# Time targets in milliseconds
TARGET_CHECKPOINT_TIMEOUT_MS=$((30 * 60 * 1000))            # 30 min

# Size targets in bytes
TARGET_MAX_WAL_SIZE_BYTES=$((32 * 1024 * 1024 * 1024))       # 32 GB

# maintenance_work_mem target scales with instance RAM per the runbook table.
# We derive it from shared_buffers as a rough proxy for instance size, then
# clamp to sensible bounds. shared_buffers itself is fetched below.
# Rule: maintenance_work_mem >= max(1 GB, 0.5 * shared_buffers) for load window.
# If shared_buffers is unavailable, fall back to a flat 1 GB minimum.

# Scale factor: LOWER-is-better
TARGET_AV_VACUUM_SCALE_FACTOR="0.05"

# ------------------------- collect actual settings ----------------------------

SB_RAW="$(fetch_setting_normalized shared_buffers)"
SB_BYTES="$(to_canonical "$SB_RAW")"

CT_RAW="$(fetch_setting_normalized checkpoint_timeout)"
CT_MS="$(to_canonical "$CT_RAW")"
CT_HUMAN="$(fetch_setting_raw checkpoint_timeout)"

MWS_RAW="$(fetch_setting_normalized max_wal_size)"
MWS_BYTES="$(to_canonical "$MWS_RAW")"
MWS_HUMAN="$(fetch_setting_raw max_wal_size)"

MWM_RAW="$(fetch_setting_normalized maintenance_work_mem)"
MWM_BYTES="$(to_canonical "$MWM_RAW")"
MWM_HUMAN="$(fetch_setting_raw maintenance_work_mem)"

AVSF_RAW="$(fetch_setting_normalized autovacuum_vacuum_scale_factor)"
AVSF_VAL="$(to_canonical "$AVSF_RAW")"

# Companion / informational settings
MIN_WAL_SIZE_H="$(fetch_setting_raw min_wal_size)"
CCT_H="$(fetch_setting_raw checkpoint_completion_target)"
WAL_COMPRESSION_H="$(fetch_setting_raw wal_compression)"
WAL_BUFFERS_H="$(fetch_setting_raw wal_buffers)"
SYNC_COMMIT_H="$(fetch_setting_raw synchronous_commit)"
AV_ENABLED_H="$(fetch_setting_raw autovacuum)"
AV_MAX_WORKERS_H="$(fetch_setting_raw autovacuum_max_workers)"
AV_NAPTIME_H="$(fetch_setting_raw autovacuum_naptime)"
AV_COST_LIMIT_H="$(fetch_setting_raw autovacuum_vacuum_cost_limit)"
AV_COST_DELAY_H="$(fetch_setting_raw autovacuum_vacuum_cost_delay)"
AV_FREEZE_MAX_H="$(fetch_setting_raw autovacuum_freeze_max_age)"
AV_WORK_MEM_H="$(fetch_setting_raw autovacuum_work_mem)"

# Derive maintenance_work_mem target now that we have shared_buffers
if [[ -n "$SB_BYTES" && "$SB_BYTES" =~ ^[0-9]+$ && "$SB_BYTES" -gt 0 ]]; then
  HALF_SB=$(( SB_BYTES / 2 ))
  ONE_GB=$(( 1 * 1024 * 1024 * 1024 ))
  if (( HALF_SB > ONE_GB )); then
    TARGET_MWM_BYTES=$HALF_SB
  else
    TARGET_MWM_BYTES=$ONE_GB
  fi
else
  TARGET_MWM_BYTES=$(( 1 * 1024 * 1024 * 1024 ))  # 1 GB fallback
fi

# ------------------------------- comparison -----------------------------------

FAIL_COUNT=0

# Fixed column widths so status column always aligns
LABEL_W=34
CURRENT_W=18
TARGET_W=22

pad() {
  # $1 = string, $2 = width. Truncates or right-pads with spaces.
  local s="$1" w="$2"
  local len=${#s}
  if (( len >= w )); then
    printf "%s" "${s:0:w}"
  else
    printf "%s%*s" "$s" $(( w - len )) ""
  fi
}

# Prints one report row.
#   $1 label
#   $2 current (raw/human string)
#   $3 target  (human string)
#   $4 status  (PASS | FAIL | INFO)
#   $5 note    (optional)
report_row() {
  local label="$1" current="$2" target="$3" status="$4" note="${5:-}"
  local color plain
  case "$status" in
    PASS) color="$GREEN"; plain="PASS" ;;
    FAIL) color="$RED";   plain="FAIL" ;;
    INFO) color="$YELLOW";plain="INFO" ;;
    *)    color="";       plain="$status" ;;
  esac
  printf "  %s  %s  %s  ${color}%-6s${RESET}" \
    "$(pad "$label" $LABEL_W)" \
    "$(pad "$current" $CURRENT_W)" \
    "$(pad "$target"  $TARGET_W)" \
    "$plain"
  if [[ -n "$note" ]]; then
    printf "  ${DIM}%s${RESET}" "$note"
  fi
  printf "\n"
}

echo "${BOLD}Critical parameters (runbook §3)${RESET}"
printf "  ${BOLD}%s  %s  %s  %-6s${RESET}\n" \
  "$(pad "Parameter" $LABEL_W)" \
  "$(pad "Current" $CURRENT_W)" \
  "$(pad "Target (min)" $TARGET_W)" \
  "Status"
printf "  %s\n" "$(printf '%.0s-' $(seq 1 90))"

# --- 1. checkpoint_timeout ----------------------------------------------------
if [[ -n "$CT_MS" && "$CT_MS" =~ ^[0-9]+$ ]]; then
  if (( CT_MS >= TARGET_CHECKPOINT_TIMEOUT_MS )); then
    report_row "checkpoint_timeout" "$CT_HUMAN" "$(human_ms $TARGET_CHECKPOINT_TIMEOUT_MS)" PASS
  else
    report_row "checkpoint_timeout" "$CT_HUMAN" "$(human_ms $TARGET_CHECKPOINT_TIMEOUT_MS)" FAIL \
      "increase to reduce full-page-write amplification"
    FAIL_COUNT=$((FAIL_COUNT+1))
  fi
else
  report_row "checkpoint_timeout" "?" "$(human_ms $TARGET_CHECKPOINT_TIMEOUT_MS)" FAIL "could not read setting"
  FAIL_COUNT=$((FAIL_COUNT+1))
fi

# --- 2. max_wal_size ----------------------------------------------------------
if [[ -n "$MWS_BYTES" && "$MWS_BYTES" =~ ^[0-9]+$ ]]; then
  if (( MWS_BYTES >= TARGET_MAX_WAL_SIZE_BYTES )); then
    report_row "max_wal_size" "$MWS_HUMAN" "$(human_bytes $TARGET_MAX_WAL_SIZE_BYTES)" PASS
  else
    report_row "max_wal_size" "$MWS_HUMAN" "$(human_bytes $TARGET_MAX_WAL_SIZE_BYTES)" FAIL \
      "raise so checkpoint_timeout fires first"
    FAIL_COUNT=$((FAIL_COUNT+1))
  fi
else
  report_row "max_wal_size" "?" "$(human_bytes $TARGET_MAX_WAL_SIZE_BYTES)" FAIL "could not read setting"
  FAIL_COUNT=$((FAIL_COUNT+1))
fi

# --- 3. maintenance_work_mem --------------------------------------------------
if [[ -n "$MWM_BYTES" && "$MWM_BYTES" =~ ^[0-9]+$ ]]; then
  if (( MWM_BYTES >= TARGET_MWM_BYTES )); then
    report_row "maintenance_work_mem" "$MWM_HUMAN" "$(human_bytes $TARGET_MWM_BYTES)" PASS \
      "target = max(1 GB, 0.5 × shared_buffers)"
  else
    report_row "maintenance_work_mem" "$MWM_HUMAN" "$(human_bytes $TARGET_MWM_BYTES)" FAIL \
      "raise for faster index builds; set autovacuum_work_mem lower to cap workers"
    FAIL_COUNT=$((FAIL_COUNT+1))
  fi
else
  report_row "maintenance_work_mem" "?" "$(human_bytes $TARGET_MWM_BYTES)" FAIL "could not read setting"
  FAIL_COUNT=$((FAIL_COUNT+1))
fi

# --- 4. autovacuum_vacuum_scale_factor ----------------------------------------
# Lower-is-better; PASS if <= target.
if [[ -n "$AVSF_VAL" ]]; then
  awk -v a="$AVSF_VAL" -v t="$TARGET_AV_VACUUM_SCALE_FACTOR" \
    'BEGIN { exit !(a+0 <= t+0) }'
  if [[ $? -eq 0 ]]; then
    report_row "autovacuum_vacuum_scale_factor" "$AVSF_VAL" "<= $TARGET_AV_VACUUM_SCALE_FACTOR" PASS
  else
    report_row "autovacuum_vacuum_scale_factor" "$AVSF_VAL" "<= $TARGET_AV_VACUUM_SCALE_FACTOR" FAIL \
      "lower to trigger vacuum sooner on write-heavy tables"
    FAIL_COUNT=$((FAIL_COUNT+1))
  fi
else
  report_row "autovacuum_vacuum_scale_factor" "?" "<= $TARGET_AV_VACUUM_SCALE_FACTOR" FAIL "could not read setting"
  FAIL_COUNT=$((FAIL_COUNT+1))
fi

# ---------------------- companion / informational rows ------------------------

echo
echo "${BOLD}Companion settings (informational — see runbook §3)${RESET}"
printf "  ${BOLD}%s  %s  %s  %-6s${RESET}\n" \
  "$(pad "Parameter" $LABEL_W)" \
  "$(pad "Current" $CURRENT_W)" \
  "$(pad "Runbook guidance" $TARGET_W)" \
  "Status"
printf "  %s\n" "$(printf '%.0s-' $(seq 1 90))"

report_row "shared_buffers"                "$(fetch_setting_raw shared_buffers)"  "~25% of RAM"      INFO
report_row "min_wal_size"                  "$MIN_WAL_SIZE_H"                       "2–4 GB"           INFO
report_row "checkpoint_completion_target"  "$CCT_H"                                "0.9"              INFO
report_row "wal_compression"               "$WAL_COMPRESSION_H"                    "on (lz4 PG15+)"   INFO
report_row "wal_buffers"                   "$WAL_BUFFERS_H"                        "64 MB"            INFO
report_row "synchronous_commit"            "$SYNC_COMMIT_H"                        "on (session=off)" INFO
report_row "autovacuum"                    "$AV_ENABLED_H"                         "on"               INFO
report_row "autovacuum_max_workers"        "$AV_MAX_WORKERS_H"                     "5–6"              INFO
report_row "autovacuum_naptime"            "$AV_NAPTIME_H"                         "15 s"             INFO
report_row "autovacuum_vacuum_cost_limit"  "$AV_COST_LIMIT_H"                      "2000"             INFO
report_row "autovacuum_vacuum_cost_delay"  "$AV_COST_DELAY_H"                      "0–1 ms"           INFO
report_row "autovacuum_freeze_max_age"     "$AV_FREEZE_MAX_H"                      "400M–800M"        INFO
report_row "autovacuum_work_mem"           "$AV_WORK_MEM_H"                        "256 MB – 1 GB"    INFO

# --------------------------------- summary ------------------------------------

echo
if (( FAIL_COUNT == 0 )); then
  echo "${GREEN}${BOLD}✔ All critical parameters match the runbook.${RESET}"
  echo "${DIM}Review the informational rows above before starting the load.${RESET}"
  echo
  exit 0
else
  echo "${RED}${BOLD}✘ ${FAIL_COUNT} critical setting(s) do not match the runbook.${RESET}"
  echo "${DIM}Update the RDS parameter group (or use ALTER SYSTEM + reload where allowed)"
  echo "and re-run this script before starting the migration.${RESET}"
  echo
  exit 1
fi
