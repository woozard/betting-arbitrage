#!/bin/bash
# Periodic disk cleanup for betting-arbitrage on EC2.
#
# Safe to run while betting-arb is running: only removes temp dirs that are
# (a) not referenced by any chrome/chromedriver process, and (b) older than
# MIN_AGE_MINUTES. Does NOT stop the scheduler by default.
#
# Usage:
#   ./cleanup_disk.sh              # normal run
#   CLEANUP_STOP_SERVICE=1 ./cleanup_disk.sh   # stop scheduler first (maintenance only)
#
# Environment overrides (optional):
#   PROJECT_DIR, MIN_AGE_MINUTES, LOG_ROTATED_AGE_DAYS, LOG_DATED_AGE_DAYS,
#   SCHEDULER_LOG_MAX_MB, CLEANUP_STOP_SERVICE

set -euo pipefail

PROJECT_DIR="${PROJECT_DIR:-$HOME/betting-arbitrage}"
MIN_AGE_MINUTES="${MIN_AGE_MINUTES:-30}"
LOG_ROTATED_AGE_DAYS="${LOG_ROTATED_AGE_DAYS:-3}"
LOG_DATED_AGE_DAYS="${LOG_DATED_AGE_DAYS:-7}"
SCHEDULER_LOG_MAX_MB="${SCHEDULER_LOG_MAX_MB:-200}"
KEEP_SCHEDULER_LOG_MB="${KEEP_SCHEDULER_LOG_MB:-10}"
CLEANUP_LOG="${PROJECT_DIR}/logs/cleanup_disk.log"

log() {
    local line="[$(date -Iseconds)] $*"
    echo "$line"
    mkdir -p "${PROJECT_DIR}/logs"
    echo "$line" >> "$CLEANUP_LOG"
}

dir_in_use() {
    local d="$1"
    pgrep -af "chrome|chromedriver" 2>/dev/null | grep -qF "$d"
}

truncate_large_scheduler_log() {
    local f="$1"
    [[ -f "$f" ]] || return 0

    local max_bytes=$((SCHEDULER_LOG_MAX_MB * 1024 * 1024))
    local keep_bytes=$((KEEP_SCHEDULER_LOG_MB * 1024 * 1024))
    local size
    size=$(stat -c%s "$f" 2>/dev/null || echo 0)

    if (( size > max_bytes )); then
        tail -c "$keep_bytes" "$f" > "${f}.truncate_tmp"
        mv "${f}.truncate_tmp" "$f"
        log "truncated scheduler log to last ${KEEP_SCHEDULER_LOG_MB}MB: $f (was ${size} bytes)"
    fi
}

main() {
    cd "$PROJECT_DIR"
    mkdir -p logs tmp

    log "=== cleanup start (min_age=${MIN_AGE_MINUTES}m) ==="
    log "disk before: $(df -h / | awk 'NR==2 {print $3 " used / " $2 " total (" $5 " full)"}')"

    if [[ "${CLEANUP_STOP_SERVICE:-0}" == "1" ]]; then
        log "stopping betting-arb (CLEANUP_STOP_SERVICE=1)"
        sudo systemctl stop betting-arb || true
        sleep 3
        pkill -f 'chromedriver' 2>/dev/null || true
        pkill -f 'google-chrome' 2>/dev/null || true
        sleep 2
    fi

    local removed_tmp=0
    shopt -s nullglob
    for d in tmp/brightdata_proxy_* tmp/chrome_user_data_*; do
        if [[ -d "$d" ]] && ! dir_in_use "$d" && find "$d" -maxdepth 0 -mmin "+${MIN_AGE_MINUTES}" 2>/dev/null | grep -q .; then
            rm -rf "$d"
            log "removed stale tmp: $d"
            removed_tmp=$((removed_tmp + 1))
        fi
    done
    shopt -u nullglob
    log "removed ${removed_tmp} stale tmp dir(s)"

    local removed_rotated
    removed_rotated=$(find logs -maxdepth 1 -type f -name '*.log.*' -mtime "+${LOG_ROTATED_AGE_DAYS}" -print -delete 2>/dev/null | wc -l | tr -d ' ')
    log "removed ${removed_rotated} rotated log backup(s) older than ${LOG_ROTATED_AGE_DAYS}d"

    local removed_dated
    removed_dated=$(find logs -maxdepth 1 -type f -name '*-20*.log' -mtime "+${LOG_DATED_AGE_DAYS}" -print -delete 2>/dev/null | wc -l | tr -d ' ')
    log "removed ${removed_dated} dated log(s) older than ${LOG_DATED_AGE_DAYS}d"

    for f in logs/arbitrage.log logs/*_odds.log logs/*_betting.log; do
        truncate_large_scheduler_log "$f"
    done

    find "$PROJECT_DIR" -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
    find "$PROJECT_DIR" -maxdepth 1 -type f -name 'debug_*.png' -mtime +3 -delete 2>/dev/null || true

    # Stale per-job flock locks (scheduler also prunes at 1h)
    find /tmp -maxdepth 1 -type f -name '*.lock' -mmin +120 -user "$(whoami)" -print -delete 2>/dev/null | while read -r lock; do
        log "removed stale lock: $lock"
    done

    # If root filesystem is still very full, vacuum systemd journal (read-only on logs)
    local use_pct
    use_pct=$(df / | awk 'NR==2 {gsub(/%/,"",$5); print $5}')
    if (( use_pct >= 90 )); then
        log "disk >= 90% full; vacuuming journal to 200M"
        sudo journalctl --vacuum-size=200M 2>/dev/null || true
    fi

    if [[ "${CLEANUP_STOP_SERVICE:-0}" == "1" ]]; then
        log "restarting betting-arb"
        sudo systemctl start betting-arb || true
    fi

    log "disk after:  $(df -h / | awk 'NR==2 {print $3 " used / " $2 " total (" $5 " full)"}')"
    log "=== cleanup end ==="
}

main "$@"