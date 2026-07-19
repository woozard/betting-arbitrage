#!/bin/bash
# Kill betting jobs whose log files have gone stale (silent Chrome hang),
# and reap orphan Selenium Chrome/chromedriver processes.
set -euo pipefail

PROJECT_DIR="${PROJECT_DIR:-$HOME/betting-arbitrage}"
LOG_DIR="${LOG_DIR:-$PROJECT_DIR/logs}"
MAX_STALE_SEC="${MAX_STALE_SEC:-600}"
STAMP="$(date -u '+%Y-%m-%d %H:%M:%S UTC')"
LOG_FILE="$LOG_DIR/betting_healthcheck.log"
TODAY="$(date -u '+%Y-%m-%d')"
CLEANUP_ORPHAN_CHROME="${CLEANUP_ORPHAN_CHROME:-1}"

mkdir -p "$LOG_DIR"

log() {
  echo "[$STAMP] $*" >>"$LOG_FILE"
}

is_job_running() {
  pgrep -f "$1" >/dev/null 2>&1
}

newest_log_for() {
  local prefix="$1"
  ls -t "$LOG_DIR/${prefix}"-"$TODAY".log "$LOG_DIR/${prefix}"-*.log 2>/dev/null | head -1
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

cleanup_orphan_chrome() {
  if [[ "$CLEANUP_ORPHAN_CHROME" != "1" ]]; then
    log "orphan chrome cleanup skipped (CLEANUP_ORPHAN_CHROME=0)"
    return
  fi
  if [[ ! -x "$PROJECT_DIR/cleanup_disk.sh" ]]; then
    log "WARN cleanup_disk.sh missing/not executable; skipping orphan chrome cleanup"
    return
  fi

  local before after
  before="$(ps -eo args= 2>/dev/null | grep -E '(chrome|chromedriver|chromium)' | grep -vE '(cleanup_disk|grep|check_betting_health)' | wc -l | tr -d ' ')"
  log "orphan chrome cleanup start (chrome≈${before})"
  # Light pass: kill orphans owned by dead Selenium sessions; keep live job browsers.
  if CLEANUP_ORPHAN_CHROME_ONLY=1 "$PROJECT_DIR/cleanup_disk.sh" >>"$LOG_FILE" 2>&1; then
    after="$(ps -eo args= 2>/dev/null | grep -E '(chrome|chromedriver|chromium)' | grep -vE '(cleanup_disk|grep|check_betting_health)' | wc -l | tr -d ' ')"
    log "orphan chrome cleanup done (chrome≈${before} → ${after})"
  else
    log "WARN orphan chrome cleanup failed (exit $?)"
  fi
}

{
  log "=== Health check (max stale ${MAX_STALE_SEC}s) ==="
  check_one "sports411-betting-mlb" "sports411_betting.py" "/tmp/sports411_betting.lock"
  check_one "betamapola-betting" "betamapola_betting.py" "/tmp/betamapola_betting.lock"
  check_one "paradisewager-betting" "paradisewager_betting.py" "/tmp/paradisewager_betting.lock"
  check_one "betwar-betting" "betwar_betting.py" "/tmp/betwar_betting.lock"
  cleanup_orphan_chrome
} >>"$LOG_FILE" 2>&1
