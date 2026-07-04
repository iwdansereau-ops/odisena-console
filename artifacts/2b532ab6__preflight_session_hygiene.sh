#!/usr/bin/env bash
# ------------------------------------------------------------------------------
# preflight_session_hygiene.sh
#
# Pre-migration session hygiene check for Amazon RDS for PostgreSQL.
# Verifies that the instance is in a clean state before starting a bulk load
# so that pre-existing sessions don't cause lock contention or connection
# exhaustion mid-migration.
#
# Categories checked (each produces a PASS / WARN / FAIL row):
#   1. Long-running active transactions
#   2. Idle-in-transaction sessions
#   3. Pending / blocked lock requests
#   4. Active connections vs. max_connections utilization
#
# Also surfaces (informational):
#   - Oldest transaction age (age of oldest xact_start)
#   - Prepared transactions (2PC) not yet committed/rolled back
#   - Replication slot lag (WAL retention risk)
#
# Output: color-coded status report. Failures print in RED, warnings in
# YELLOW, passes in GREEN. Details of offending sessions are printed under
# each failing category so you can decide what to terminate.
#
# Usage:
#   ./preflight_session_hygiene.sh \
#       --host mydb.xxxxx.us-east-1.rds.amazonaws.com \
#       --port 5432 \
#       --user dbadmin \
#       --db   postgres
#
# Password: PGPASSWORD env var, ~/.pgpass, or interactive prompt (whatever
# psql itself would use). No secrets are logged.
#
# Tunable thresholds via flags:
#   --long-xact-secs SECS       (default 300 = 5 min)
#   --idle-in-txn-secs SECS     (default 60)
#   --conn-warn-pct PCT         (default 60)
#   --conn-fail-pct PCT         (default 80)
#   --lock-wait-secs SECS       (default 30)
#
# Exit code:
#   0  all categories PASS
#   1  one or more categories FAIL
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

LONG_XACT_SECS=300      # active txns older than this trigger FAIL
IDLE_IN_TXN_SECS=60     # idle-in-transaction older than this triggers FAIL
CONN_WARN_PCT=60        # >= this % of max_connections = WARN
CONN_FAIL_PCT=80        # >= this % of max_connections = FAIL
LOCK_WAIT_SECS=30       # a session waiting on a lock for this long = FAIL

usage() {
  cat <<EOF
Usage: $(basename "$0") [--host H] [--port P] [--user U] [--db D] [options]

Pre-migration session hygiene checks for RDS PostgreSQL.

Connection options:
  --host              RDS endpoint (or set PGHOST)
  --port              Port (default: 5432, or PGPORT)
  --user              DB user (default: postgres, or PGUSER)
  --db                Database name (default: postgres, or PGDATABASE)

Threshold options:
  --long-xact-secs    Active txns older than N seconds fail (default: ${LONG_XACT_SECS})
  --idle-in-txn-secs  Idle-in-txn sessions older than N seconds fail (default: ${IDLE_IN_TXN_SECS})
  --lock-wait-secs    A session waiting on a lock for N seconds fails (default: ${LOCK_WAIT_SECS})
  --conn-warn-pct     % of max_connections that triggers WARN (default: ${CONN_WARN_PCT})
  --conn-fail-pct     % of max_connections that triggers FAIL (default: ${CONN_FAIL_PCT})

Output options:
  --no-color          Disable ANSI colors
  -h, --help          Show this help

Password: set PGPASSWORD, use ~/.pgpass, or enter interactively.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --host)              PGHOST="$2"; shift 2 ;;
    --port)              PGPORT="$2"; shift 2 ;;
    --user)              PGUSER="$2"; shift 2 ;;
    --db)                PGDATABASE="$2"; shift 2 ;;
    --long-xact-secs)    LONG_XACT_SECS="$2"; shift 2 ;;
    --idle-in-txn-secs)  IDLE_IN_TXN_SECS="$2"; shift 2 ;;
    --lock-wait-secs)    LOCK_WAIT_SECS="$2"; shift 2 ;;
    --conn-warn-pct)     CONN_WARN_PCT="$2"; shift 2 ;;
    --conn-fail-pct)     CONN_FAIL_PCT="$2"; shift 2 ;;
    --no-color)          NO_COLOR=1; shift ;;
    -h|--help)           usage; exit 0 ;;
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

# Numeric sanity
for v in LONG_XACT_SECS IDLE_IN_TXN_SECS LOCK_WAIT_SECS CONN_WARN_PCT CONN_FAIL_PCT; do
  if ! [[ "${!v}" =~ ^[0-9]+$ ]]; then
    echo "ERROR: $v must be a non-negative integer, got '${!v}'." >&2
    exit 2
  fi
done

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
echo "${BOLD}${CYAN}RDS PostgreSQL Pre-Migration Session Hygiene Check${RESET}"
echo "${DIM}Endpoint: ${PGUSER}@${PGHOST}:${PGPORT}/${PGDATABASE}${RESET}"
echo "${DIM}Thresholds: long_xact>${LONG_XACT_SECS}s | idle_in_txn>${IDLE_IN_TXN_SECS}s | lock_wait>${LOCK_WAIT_SECS}s | conn warn/fail=${CONN_WARN_PCT}%/${CONN_FAIL_PCT}%${RESET}"
echo

if ! psql -Xqt -v ON_ERROR_STOP=1 -c "SELECT 1;" >/dev/null 2>&1; then
  echo "${RED}ERROR:${RESET} Cannot connect to ${PGHOST}:${PGPORT} as ${PGUSER}." >&2
  echo "Check network reachability, security groups, credentials, and TLS." >&2
  exit 2
fi

PG_VERSION="$(psql -XAqt -c "SHOW server_version;" 2>/dev/null | tr -d '[:space:]')"
echo "${DIM}Server version: ${PG_VERSION}${RESET}"
echo

# ----------------------------- helper functions -------------------------------

# Prints one summary row.
#   $1 label
#   $2 metric (count or value)
#   $3 threshold
#   $4 status  (PASS | WARN | FAIL | INFO)
#   $5 note    (optional)
LABEL_W=38
METRIC_W=18
THRESH_W=22

pad() {
  local s="$1" w="$2"
  local len=${#s}
  if (( len >= w )); then
    printf "%s" "${s:0:w}"
  else
    printf "%s%*s" "$s" $(( w - len )) ""
  fi
}

report_row() {
  local label="$1" metric="$2" thresh="$3" status="$4" note="${5:-}"
  local color plain
  case "$status" in
    PASS) color="$GREEN";  plain="PASS" ;;
    WARN) color="$YELLOW"; plain="WARN" ;;
    FAIL) color="$RED";    plain="FAIL" ;;
    INFO) color="$YELLOW"; plain="INFO" ;;
    *)    color="";        plain="$status" ;;
  esac
  printf "  %s  %s  %s  ${color}%-6s${RESET}" \
    "$(pad "$label" $LABEL_W)" \
    "$(pad "$metric" $METRIC_W)" \
    "$(pad "$thresh" $THRESH_W)" \
    "$plain"
  if [[ -n "$note" ]]; then
    printf "  ${DIM}%s${RESET}" "$note"
  fi
  printf "\n"
}

# Runs a psql query, returns tab-separated output (no header, no footer).
psql_query() {
  psql -XAqt -F $'\t' -c "$1" 2>/dev/null
}

# Print a header for the "critical checks" table
print_header() {
  echo
  echo "${BOLD}$1${RESET}"
  printf "  ${BOLD}%s  %s  %s  %-6s${RESET}\n" \
    "$(pad "Check" $LABEL_W)" \
    "$(pad "Observed" $METRIC_W)" \
    "$(pad "Threshold" $THRESH_W)" \
    "Status"
  printf "  %s\n" "$(printf '%.0s-' $(seq 1 96))"
}

FAIL_COUNT=0
WARN_COUNT=0

# ----------------------- 1. Long-running active transactions ------------------

print_header "Session hygiene checks"

LONG_XACT_COUNT="$(psql_query "
  SELECT COUNT(*)
    FROM pg_stat_activity
   WHERE state IN ('active','idle in transaction','idle in transaction (aborted)')
     AND xact_start IS NOT NULL
     AND pid <> pg_backend_pid()
     AND backend_type = 'client backend'
     AND EXTRACT(EPOCH FROM (now() - xact_start)) > ${LONG_XACT_SECS}
     AND state = 'active';" | tr -d '[:space:]')"

LONG_XACT_COUNT="${LONG_XACT_COUNT:-0}"

if [[ "$LONG_XACT_COUNT" =~ ^[0-9]+$ ]] && (( LONG_XACT_COUNT == 0 )); then
  report_row "1. Long-running active transactions" "0" ">${LONG_XACT_SECS}s = FAIL" PASS
else
  report_row "1. Long-running active transactions" "$LONG_XACT_COUNT" ">${LONG_XACT_SECS}s = FAIL" FAIL \
    "may block DDL / hold row locks during load"
  FAIL_COUNT=$((FAIL_COUNT+1))
fi

# ----------------------- 2. Idle-in-transaction sessions ----------------------

IDLE_IN_TXN_COUNT="$(psql_query "
  SELECT COUNT(*)
    FROM pg_stat_activity
   WHERE state IN ('idle in transaction','idle in transaction (aborted)')
     AND pid <> pg_backend_pid()
     AND backend_type = 'client backend'
     AND xact_start IS NOT NULL
     AND EXTRACT(EPOCH FROM (now() - COALESCE(state_change, xact_start))) > ${IDLE_IN_TXN_SECS};" \
   | tr -d '[:space:]')"

IDLE_IN_TXN_COUNT="${IDLE_IN_TXN_COUNT:-0}"

if [[ "$IDLE_IN_TXN_COUNT" =~ ^[0-9]+$ ]] && (( IDLE_IN_TXN_COUNT == 0 )); then
  report_row "2. Idle-in-transaction sessions" "0" ">${IDLE_IN_TXN_SECS}s = FAIL" PASS
else
  report_row "2. Idle-in-transaction sessions" "$IDLE_IN_TXN_COUNT" ">${IDLE_IN_TXN_SECS}s = FAIL" FAIL \
    "holds locks and pins xmin, blocks autovacuum"
  FAIL_COUNT=$((FAIL_COUNT+1))
fi

# ---------------------- 3. Pending / blocked lock requests --------------------

# Any client backend that has been waiting on a lock for > LOCK_WAIT_SECS.
BLOCKED_COUNT="$(psql_query "
  SELECT COUNT(*)
    FROM pg_stat_activity a
   WHERE a.wait_event_type = 'Lock'
     AND a.pid <> pg_backend_pid()
     AND a.backend_type = 'client backend'
     AND EXTRACT(EPOCH FROM (now() - a.state_change)) > ${LOCK_WAIT_SECS};" \
  | tr -d '[:space:]')"

BLOCKED_COUNT="${BLOCKED_COUNT:-0}"

# Any ungranted lock at all (regardless of wait time) is worth an INFO count
UNGRANTED_LOCKS="$(psql_query "
  SELECT COUNT(*) FROM pg_locks WHERE NOT granted;" | tr -d '[:space:]')"
UNGRANTED_LOCKS="${UNGRANTED_LOCKS:-0}"

if [[ "$BLOCKED_COUNT" =~ ^[0-9]+$ ]] && (( BLOCKED_COUNT == 0 )); then
  if [[ "$UNGRANTED_LOCKS" =~ ^[0-9]+$ ]] && (( UNGRANTED_LOCKS > 0 )); then
    report_row "3. Sessions blocked on locks" "0" ">${LOCK_WAIT_SECS}s = FAIL" PASS \
      "${UNGRANTED_LOCKS} lock requests pending but under threshold"
  else
    report_row "3. Sessions blocked on locks" "0" ">${LOCK_WAIT_SECS}s = FAIL" PASS
  fi
else
  report_row "3. Sessions blocked on locks" "$BLOCKED_COUNT" ">${LOCK_WAIT_SECS}s = FAIL" FAIL \
    "will deadlock or delay migration DDL"
  FAIL_COUNT=$((FAIL_COUNT+1))
fi

# ---------------- 4. Active connections vs. max_connections -------------------

# We want two numbers: total connections (all backends the user can see) and
# max_connections. We deliberately count all backends (including background
# workers) because they all consume a slot toward max_connections.

CONN_LINE="$(psql_query "
  SELECT
    (SELECT COUNT(*) FROM pg_stat_activity) AS current_total,
    (SELECT COUNT(*) FROM pg_stat_activity WHERE state = 'active') AS current_active,
    current_setting('max_connections')::int AS max_conn,
    current_setting('superuser_reserved_connections')::int AS reserved;")"

CURRENT_TOTAL="$(echo "$CONN_LINE" | awk -F'\t' '{print $1}' | tr -d '[:space:]')"
CURRENT_ACTIVE="$(echo "$CONN_LINE" | awk -F'\t' '{print $2}' | tr -d '[:space:]')"
MAX_CONN="$(echo "$CONN_LINE" | awk -F'\t' '{print $3}' | tr -d '[:space:]')"
RESERVED="$(echo "$CONN_LINE" | awk -F'\t' '{print $4}' | tr -d '[:space:]')"

if [[ -z "$CURRENT_TOTAL" || -z "$MAX_CONN" || "$MAX_CONN" == "0" ]]; then
  report_row "4. Connection utilization" "?" "${CONN_WARN_PCT}/${CONN_FAIL_PCT}%" FAIL \
    "could not read pg_stat_activity or max_connections"
  FAIL_COUNT=$((FAIL_COUNT+1))
  CONN_PCT="?"
else
  CONN_PCT="$(awk -v c="$CURRENT_TOTAL" -v m="$MAX_CONN" 'BEGIN{printf "%.1f", (c*100.0)/m}')"
  METRIC="${CURRENT_TOTAL}/${MAX_CONN} (${CONN_PCT}%)"
  THRESH="warn ${CONN_WARN_PCT}% / fail ${CONN_FAIL_PCT}%"

  # Integer compare of the whole-percent part
  CONN_PCT_INT="$(awk -v p="$CONN_PCT" 'BEGIN{printf "%d", p+0}')"

  if (( CONN_PCT_INT >= CONN_FAIL_PCT )); then
    report_row "4. Connection utilization"        "$METRIC" "$THRESH" FAIL \
      "risk of connection exhaustion mid-load; add pooler or drain apps"
    FAIL_COUNT=$((FAIL_COUNT+1))
  elif (( CONN_PCT_INT >= CONN_WARN_PCT )); then
    report_row "4. Connection utilization"        "$METRIC" "$THRESH" WARN \
      "leave headroom for loader + autovacuum workers"
    WARN_COUNT=$((WARN_COUNT+1))
  else
    report_row "4. Connection utilization"        "$METRIC" "$THRESH" PASS \
      "active=${CURRENT_ACTIVE}, superuser_reserved=${RESERVED}"
  fi
fi

# ----------------------------- Informational rows -----------------------------

print_header "Informational (not gating)"

# Oldest transaction age (any state), regardless of threshold
OLDEST_XACT="$(psql_query "
  SELECT COALESCE(
           EXTRACT(EPOCH FROM (now() - MIN(xact_start)))::bigint::text,
           '0')
    FROM pg_stat_activity
   WHERE xact_start IS NOT NULL
     AND backend_type = 'client backend'
     AND pid <> pg_backend_pid();" | tr -d '[:space:]')"
OLDEST_XACT="${OLDEST_XACT:-0}"

if [[ "$OLDEST_XACT" =~ ^[0-9]+$ ]] && (( OLDEST_XACT > 0 )); then
  # human-friendly
  HUMAN="$(awk -v s="$OLDEST_XACT" 'BEGIN{
    if (s<60) printf "%d s", s;
    else if (s<3600) printf "%d min %d s", int(s/60), s%60;
    else printf "%d h %d min", int(s/3600), int((s%3600)/60);
  }')"
  report_row "Oldest active transaction age" "$HUMAN" "reference" INFO
else
  report_row "Oldest active transaction age" "none" "reference" INFO
fi

# Prepared (2PC) transactions
PREPARED_XACT="$(psql_query "SELECT COUNT(*) FROM pg_prepared_xacts;" | tr -d '[:space:]')"
PREPARED_XACT="${PREPARED_XACT:-0}"
if [[ "$PREPARED_XACT" =~ ^[0-9]+$ ]] && (( PREPARED_XACT > 0 )); then
  report_row "Prepared (2PC) transactions" "$PREPARED_XACT" "should be 0" WARN \
    "commit or rollback before load; they hold xmin indefinitely"
  WARN_COUNT=$((WARN_COUNT+1))
else
  report_row "Prepared (2PC) transactions" "0" "should be 0" PASS
fi

# Replication slots holding WAL
SLOT_LAG_ROWS="$(psql_query "
  SELECT COUNT(*)
    FROM pg_replication_slots
   WHERE (active = false)
      OR (restart_lsn IS NOT NULL
          AND pg_wal_lsn_diff(pg_current_wal_lsn(), restart_lsn) > 1073741824);" \
  | tr -d '[:space:]')"
SLOT_LAG_ROWS="${SLOT_LAG_ROWS:-0}"

TOTAL_SLOTS="$(psql_query "SELECT COUNT(*) FROM pg_replication_slots;" | tr -d '[:space:]')"
TOTAL_SLOTS="${TOTAL_SLOTS:-0}"

if [[ "$SLOT_LAG_ROWS" =~ ^[0-9]+$ ]] && (( SLOT_LAG_ROWS > 0 )); then
  report_row "Replication slots (inactive or >1GB lag)" "${SLOT_LAG_ROWS}/${TOTAL_SLOTS}" "0 problematic" WARN \
    "inactive/lagging slots pin WAL and can fill disk during load"
  WARN_COUNT=$((WARN_COUNT+1))
else
  report_row "Replication slots (inactive or >1GB lag)" "0/${TOTAL_SLOTS}" "0 problematic" PASS
fi

# Autovacuum workers currently running (informational)
AV_RUNNING="$(psql_query "
  SELECT COUNT(*) FROM pg_stat_activity
   WHERE backend_type = 'autovacuum worker';" | tr -d '[:space:]')"
AV_RUNNING="${AV_RUNNING:-0}"
report_row "Autovacuum workers running now" "$AV_RUNNING" "reference" INFO

# ------------------------------ Detail sections -------------------------------
# Only print details for FAILing categories so operators know what to kill.

print_details_header() {
  echo
  echo "${BOLD}${CYAN}$1${RESET}"
}

if [[ "$LONG_XACT_COUNT" =~ ^[0-9]+$ ]] && (( LONG_XACT_COUNT > 0 )); then
  print_details_header "Detail: long-running active transactions (top 10)"
  psql -X -P pager=off -c "
    SELECT pid,
           usename,
           application_name,
           client_addr,
           state,
           EXTRACT(EPOCH FROM (now() - xact_start))::int AS xact_age_s,
           EXTRACT(EPOCH FROM (now() - query_start))::int AS query_age_s,
           wait_event_type || ':' || COALESCE(wait_event,'') AS wait,
           LEFT(regexp_replace(query, '\s+', ' ', 'g'), 100) AS query
      FROM pg_stat_activity
     WHERE state = 'active'
       AND xact_start IS NOT NULL
       AND pid <> pg_backend_pid()
       AND backend_type = 'client backend'
       AND EXTRACT(EPOCH FROM (now() - xact_start)) > ${LONG_XACT_SECS}
     ORDER BY xact_start
     LIMIT 10;" 2>/dev/null
  echo "${DIM}Terminate with: SELECT pg_terminate_backend(<pid>);${RESET}"
fi

if [[ "$IDLE_IN_TXN_COUNT" =~ ^[0-9]+$ ]] && (( IDLE_IN_TXN_COUNT > 0 )); then
  print_details_header "Detail: idle-in-transaction sessions (top 10)"
  psql -X -P pager=off -c "
    SELECT pid,
           usename,
           application_name,
           client_addr,
           state,
           EXTRACT(EPOCH FROM (now() - COALESCE(state_change, xact_start)))::int AS idle_s,
           EXTRACT(EPOCH FROM (now() - xact_start))::int AS xact_age_s,
           LEFT(regexp_replace(query, '\s+', ' ', 'g'), 100) AS last_query
      FROM pg_stat_activity
     WHERE state IN ('idle in transaction','idle in transaction (aborted)')
       AND pid <> pg_backend_pid()
       AND backend_type = 'client backend'
       AND EXTRACT(EPOCH FROM (now() - COALESCE(state_change, xact_start))) > ${IDLE_IN_TXN_SECS}
     ORDER BY COALESCE(state_change, xact_start)
     LIMIT 10;" 2>/dev/null
  echo "${DIM}Fix at the app layer or terminate with pg_terminate_backend(<pid>).${RESET}"
fi

if [[ "$BLOCKED_COUNT" =~ ^[0-9]+$ ]] && (( BLOCKED_COUNT > 0 )); then
  print_details_header "Detail: blocked sessions and their blockers (top 10)"
  psql -X -P pager=off -c "
    SELECT blocked.pid                                    AS blocked_pid,
           blocked.usename                                AS blocked_user,
           EXTRACT(EPOCH FROM (now() - blocked.state_change))::int AS waiting_s,
           blocked.wait_event_type || ':' || COALESCE(blocked.wait_event,'') AS wait,
           LEFT(regexp_replace(blocked.query,'\s+',' ','g'), 60) AS blocked_query,
           blocker_pid,
           blocker.usename                                AS blocker_user,
           LEFT(regexp_replace(blocker.query,'\s+',' ','g'), 60) AS blocker_query
      FROM pg_stat_activity blocked
      CROSS JOIN LATERAL unnest(pg_blocking_pids(blocked.pid)) AS blocker_pid
      JOIN pg_stat_activity blocker ON blocker.pid = blocker_pid
     WHERE blocked.wait_event_type = 'Lock'
       AND blocked.pid <> pg_backend_pid()
       AND blocked.backend_type = 'client backend'
       AND EXTRACT(EPOCH FROM (now() - blocked.state_change)) > ${LOCK_WAIT_SECS}
     ORDER BY blocked.state_change
     LIMIT 10;" 2>/dev/null
  echo "${DIM}Terminate the *blocker* PID (not the waiter) to release the lock chain.${RESET}"
fi

# Connection breakdown when at or over WARN threshold
if [[ -n "${CURRENT_TOTAL:-}" && "$CURRENT_TOTAL" =~ ^[0-9]+$ && -n "${MAX_CONN:-}" && "$MAX_CONN" =~ ^[0-9]+$ ]]; then
  CONN_PCT_INT="$(awk -v c="$CURRENT_TOTAL" -v m="$MAX_CONN" 'BEGIN{printf "%d", (c*100.0)/m}')"
  if (( CONN_PCT_INT >= CONN_WARN_PCT )); then
    print_details_header "Detail: connection breakdown by (usename, application_name, state)"
    psql -X -P pager=off -c "
      SELECT COALESCE(usename,'<none>')          AS usename,
             COALESCE(application_name,'<none>') AS application,
             COALESCE(state, backend_type)       AS state,
             COUNT(*)                            AS sessions
        FROM pg_stat_activity
       GROUP BY 1,2,3
       ORDER BY sessions DESC
       LIMIT 15;" 2>/dev/null
    echo "${DIM}Route apps through a pooler (pgbouncer / RDS Proxy) before the load starts.${RESET}"
  fi
fi

# --------------------------------- summary ------------------------------------

echo
if (( FAIL_COUNT == 0 && WARN_COUNT == 0 )); then
  echo "${GREEN}${BOLD}✔ All session hygiene checks passed. Instance is clean for bulk load.${RESET}"
  echo
  exit 0
elif (( FAIL_COUNT == 0 )); then
  echo "${YELLOW}${BOLD}▲ ${WARN_COUNT} warning(s) — review details above before starting the load.${RESET}"
  echo "${DIM}No blocking failures; you may proceed with caution.${RESET}"
  echo
  exit 0
else
  echo "${RED}${BOLD}✘ ${FAIL_COUNT} category(ies) FAILED (${WARN_COUNT} warning(s)).${RESET}"
  echo "${DIM}Clear the sessions listed above before starting the migration. Re-run this${RESET}"
  echo "${DIM}script until all critical categories PASS.${RESET}"
  echo
  exit 1
fi
