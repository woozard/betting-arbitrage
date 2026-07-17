#!/bin/bash
# Kill betting jobs whose log files have gone stale (silent Chrome hang).
set -euo pipefail

PROJECT_DIR="${PROJECT_DIR:-$HOME/betting-arbitrage}"
LOG_DIR="${LOG_DIR:-$PROJECT_DIR/logs}"
MAX_STALE_SEC="${MAX_STALE_SEC:-600}"
STAMP="$(date -u '+%Y-%m-%d %H:%M:%S UTC')"
LOG_FILE="$LOG_DIR/betting_healthcheck.log"
TODAY="$(date -u '+%Y-%m-%d')"

mkdir -p "$LOG_DIR"

log() {
  echo "[$STAMP] $*" >>"$LOG_FILE"
}

is_job_running() {
  pgrep -f "$1" >/dev/null 2>&1
}

newest_log_for() {
  local prefix="$1"
  # Prefer scheduler job logs (sports411_betting_wnba.log) then controller daily logs.
  ls -t \
    "$LOG_DIR/${prefix}".log \
    "$LOG_DIR/${prefix}"-"$TODAY".log \
    "$LOG_DIR/${prefix}"-*.log \
    2>/dev/null | head -1
}

check_one() {
  local log_prefix="$1"
  local job_pattern="$2"
  local lock_file="$3"

  if ! is_job_running "$job_pattern"; then
    return
  fi

  local log_file
  log_file="$(newest_log_for "$log_prefix")"
  if [[ -z "$log_file" || ! -f "$log_file" ]]; then
    log "WARN $job_pattern running but no log file found; restarting"
    pkill -f "$job_pattern" || true
    rm -f "$lock_file"
    return
  fi

  local age=$(( $(date +%s) - $(stat -c %Y "$log_file") ))
  if (( age > MAX_STALE_SEC )); then
    log "STALE $job_pattern log $log_file unchanged for ${age}s; restarting"
    pkill -f "$job_pattern" || true
    sleep 2
    pkill -9 -f "$job_pattern" || true
    rm -f "$lock_file"
  fi
}

{
  log "=== Health check (max stale ${MAX_STALE_SEC}s) ==="
  # Match stack runners via unique flock lock names in process table is hard;
  # use script name + stack-specific lock files.
  check_one "sports411_betting_wnba" "run_stack_job.sh wnba sports411_betting.py" "/tmp/sports411_betting_wnba.lock"
  check_one "sports411_betting_mlb" "run_stack_job.sh mlb sports411_betting.py" "/tmp/sports411_betting_mlb.lock"
  check_one "betamapola_betting_wnba" "run_stack_job.sh wnba betamapola_betting.py" "/tmp/betamapola_betting_wnba.lock"
  check_one "betamapola_betting_mlb" "run_stack_job.sh mlb betamapola_betting.py" "/tmp/betamapola_betting_mlb.lock"
  check_one "fourcasters_betting_wnba" "run_stack_job.sh wnba fourcasters_betting.py" "/tmp/fourcasters_betting_wnba.lock"
  check_one "fourcasters_betting_mlb" "run_stack_job.sh mlb fourcasters_betting.py" "/tmp/fourcasters_betting_mlb.lock"
} >>"$LOG_FILE" 2>&1
