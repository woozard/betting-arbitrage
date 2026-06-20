#!/bin/bash
# Periodic server maintenance for betting-arbitrage on EC2.
#
# Safe to run while betting-arb is running: only removes temp dirs that are
# (a) not referenced by any chrome/chromedriver/python process, and (b) older than
# MIN_AGE_MINUTES (unless over MAX_TMP_PROFILES cap). Does NOT stop the scheduler
# by default.
#
# Usage:
#   ./cleanup_disk.sh              # normal run
#   CLEANUP_STOP_SERVICE=1 ./cleanup_disk.sh   # stop scheduler first (maintenance only)
#
# Environment overrides (optional):
#   PROJECT_DIR, MIN_AGE_MINUTES, MAX_TMP_PROFILES, LOG_ROTATED_AGE_DAYS,
#   LOG_DATED_AGE_DAYS, DEBUG_MAX_AGE_HOURS, DEBUG_MAX_MB,
#   SCHEDULER_LOG_MAX_MB, KEEP_SCHEDULER_LOG_MB, JOURNAL_MAX_MB, DISK_WARN_PCT,
#   CLEANUP_STOP_SERVICE

set -euo pipefail

PROJECT_DIR="${PROJECT_DIR:-$HOME/betting-arbitrage}"
MIN_AGE_MINUTES="${MIN_AGE_MINUTES:-20}"
MAX_TMP_PROFILES="${MAX_TMP_PROFILES:-12}"
LOG_ROTATED_AGE_DAYS="${LOG_ROTATED_AGE_DAYS:-2}"
LOG_DATED_AGE_DAYS="${LOG_DATED_AGE_DAYS:-3}"
DEBUG_MAX_AGE_HOURS="${DEBUG_MAX_AGE_HOURS:-24}"
DEBUG_MAX_MB="${DEBUG_MAX_MB:-80}"
SCHEDULER_LOG_MAX_MB="${SCHEDULER_LOG_MAX_MB:-80}"
KEEP_SCHEDULER_LOG_MB="${KEEP_SCHEDULER_LOG_MB:-8}"
MAINT_LOG_MAX_MB="${MAINT_LOG_MAX_MB:-2}"
JOURNAL_MAX_MB="${JOURNAL_MAX_MB:-150}"
DISK_WARN_PCT="${DISK_WARN_PCT:-80}"
CLEANUP_LOG="${PROJECT_DIR}/logs/cleanup_disk.log"

log() {
    local line="[$(date -Iseconds)] $*"
    echo "$line"
    mkdir -p "${PROJECT_DIR}/logs"
    echo "$line" >> "$CLEANUP_LOG"
}

truncate_file_tail() {
    local f="$1"
    local max_mb="$2"
    local keep_mb="$3"
    [[ -f "$f" ]] || return 0

    local max_bytes=$((max_mb * 1024 * 1024))
    local keep_bytes=$((keep_mb * 1024 * 1024))
    local size
    size=$(stat -c%s "$f" 2>/dev/null || echo 0)

    if (( size > max_bytes )); then
        tail -c "$keep_bytes" "$f" > "${f}.truncate_tmp"
        mv "${f}.truncate_tmp" "$f"
        log "truncated log to last ${keep_mb}MB: $f (was ${size} bytes)"
    fi
}

collect_active_tmp_dirs() {
    # Paths currently referenced by chrome / chromedriver / betting jobs.
    local active=()
    local line path
    while IFS= read -r line; do
        for path in $(echo "$line" | grep -oE "${PROJECT_DIR}/tmp/(chrome_user_data_[^ ]+|brightdata_proxy_[^ ]+)" || true); do
            active+=("$path")
        done
    done < <(pgrep -af 'chrome|chromedriver|_betting\.py|_odds\.py' 2>/dev/null || true)

    if ((${#active[@]})); then
        printf '%s\n' "${active[@]}" | sort -u
    fi
}

dir_is_active() {
    local d="$1"
    local active
    active="$(collect_active_tmp_dirs)"
    [[ -n "$active" ]] && echo "$active" | grep -qxF "$d"
}

cleanup_tmp_profiles() {
    local removed=0
    local min_age="$MIN_AGE_MINUTES"
    local -a active_dirs=()
    local -a all_dirs=()

    mapfile -t active_dirs < <(collect_active_tmp_dirs || true)

    shopt -s nullglob
    for pat in tmp/chrome_user_data_* tmp/brightdata_proxy_*; do
        [[ -d "$pat" ]] || continue
        all_dirs+=("$pat")
    done
    shopt -u nullglob

    if ((${#all_dirs[@]} == 0)); then
        log "no tmp profile dirs found"
        return 0
    fi

    # Newest first (keep recent profiles even when over cap).
    mapfile -t all_dirs < <(
        for d in "${all_dirs[@]}"; do
            printf '%s %s\n' "$(stat -c %Y "$d" 2>/dev/null || echo 0)" "$d"
        done | sort -rn | awk '{print $2}'
    )

    local idx=0
    for d in "${all_dirs[@]}"; do
        idx=$((idx + 1))
        local reason=""

        if dir_is_active "$d"; then
            continue
        fi

        if (( idx > MAX_TMP_PROFILES )); then
            reason="over cap (${MAX_TMP_PROFILES})"
        elif find "$d" -maxdepth 0 -mmin "+${min_age}" 2>/dev/null | grep -q .; then
            reason="stale (>${min_age}m)"
        else
            continue
        fi

        rm -rf "$d"
        log "removed tmp dir (${reason}): $d"
        removed=$((removed + 1))
    done

    log "removed ${removed} tmp profile dir(s); ${#all_dirs[@]} total before cleanup"
}

prune_debug_artifacts() {
    local debug_dir="${PROJECT_DIR}/logs/debug"
    [[ -d "$debug_dir" ]] || return 0

    local removed_age=0
    local max_age_min=$((DEBUG_MAX_AGE_HOURS * 60))
    while IFS= read -r f; do
        rm -f "$f"
        removed_age=$((removed_age + 1))
    done < <(find "$debug_dir" -type f -mmin "+${max_age_min}" 2>/dev/null || true)
    log "removed ${removed_age} debug file(s) older than ${DEBUG_MAX_AGE_HOURS}h"

    local max_bytes=$((DEBUG_MAX_MB * 1024 * 1024))
    local total
    total=$(du -sb "$debug_dir" 2>/dev/null | awk '{print $1}')
    if (( total > max_bytes )); then
        local removed_cap=0
        while IFS= read -r f; do
            [[ -f "$f" ]] || continue
            rm -f "$f"
            removed_cap=$((removed_cap + 1))
            total=$(du -sb "$debug_dir" 2>/dev/null | awk '{print $1}')
            (( total <= max_bytes )) && break
        done < <(find "$debug_dir" -type f -printf '%T@ %p\n' 2>/dev/null | sort -n | awk '{print $2}')
        log "removed ${removed_cap} debug file(s) to cap logs/debug under ${DEBUG_MAX_MB}MB"
    fi
}

run_python_temp_cleanup() {
    if [[ -x "${PROJECT_DIR}/venv/bin/python3" ]]; then
        (
            cd "$PROJECT_DIR"
            PYTHONPATH="$PROJECT_DIR" "${PROJECT_DIR}/venv/bin/python3" - <<'PY' || true
from utils.chrome_temp import cleanup_stale_temp_dirs
cleanup_stale_temp_dirs(max_age_seconds=1200)
PY
        )
        log "ran python chrome_temp cleanup (max_age=20m)"
    fi
}

vacuum_journal_if_needed() {
    local use_pct="$1"
    if (( use_pct >= DISK_WARN_PCT )); then
        log "disk >= ${DISK_WARN_PCT}% full; vacuuming systemd journal to ${JOURNAL_MAX_MB}M"
        sudo journalctl --vacuum-size="${JOURNAL_MAX_MB}M" 2>/dev/null || true
    fi
}

log_health_snapshot() {
    local use_pct="$1"
    local svc mem avail
    svc="$(systemctl is-active betting-arb 2>/dev/null || echo unknown)"
    mem="$(free -h | awk '/^Mem:/ {print $3 "/" $2 " used, " $7 " avail"}')"
    avail="$(df -h / | awk 'NR==2 {print $4 " free (" $5 " used)"}')"
    log "health: betting-arb=${svc} | mem=${mem} | disk=${avail}"
    if (( use_pct >= DISK_WARN_PCT )); then
        log "WARN: root filesystem at ${use_pct}% — review logs/tmp growth"
    fi
}

main() {
    cd "$PROJECT_DIR"
    mkdir -p logs/debug tmp

    log "=== maintenance start (min_age=${MIN_AGE_MINUTES}m, max_profiles=${MAX_TMP_PROFILES}) ==="
    log "disk before: $(df -h / | awk 'NR==2 {print $3 " used / " $2 " total (" $5 " full)"}')"

    local use_pct
    use_pct=$(df / | awk 'NR==2 {gsub(/%/,"",$5); print $5}')

    if [[ "${CLEANUP_STOP_SERVICE:-0}" == "1" ]]; then
        log "stopping betting-arb (CLEANUP_STOP_SERVICE=1)"
        sudo systemctl stop betting-arb || true
        sleep 3
        pkill -f 'chromedriver' 2>/dev/null || true
        pkill -f 'google-chrome' 2>/dev/null || true
        sleep 2
    fi

    cleanup_tmp_profiles
    run_python_temp_cleanup

    local removed_rotated removed_dated removed_fetch
    removed_rotated=$(find logs -maxdepth 1 -type f -name '*.log.*' -mtime "+${LOG_ROTATED_AGE_DAYS}" -print -delete 2>/dev/null | wc -l | tr -d ' ')
    log "removed ${removed_rotated} rotated log backup(s) older than ${LOG_ROTATED_AGE_DAYS}d"

    removed_dated=$(find logs -maxdepth 1 -type f -name '*-20*.log' -mtime "+${LOG_DATED_AGE_DAYS}" -print -delete 2>/dev/null | wc -l | tr -d ' ')
    log "removed ${removed_dated} dated controller log(s) older than ${LOG_DATED_AGE_DAYS}d"

    removed_fetch=$(find logs -maxdepth 1 -type f -name '*-fetch-odds-*.log' -mtime "+${LOG_DATED_AGE_DAYS}" -print -delete 2>/dev/null | wc -l | tr -d ' ')
    log "removed ${removed_fetch} fetch-odds log(s) older than ${LOG_DATED_AGE_DAYS}d"

    prune_debug_artifacts

    shopt -s nullglob
    for f in logs/arbitrage.log logs/*_odds.log logs/*_betting.log logs/*-fetch-odds-*.log; do
        truncate_file_tail "$f" "$SCHEDULER_LOG_MAX_MB" "$KEEP_SCHEDULER_LOG_MB"
    done
    shopt -u nullglob

    truncate_file_tail "$CLEANUP_LOG" "$MAINT_LOG_MAX_MB" 1
    truncate_file_tail "${PROJECT_DIR}/logs/betting_healthcheck.log" 20 2
    truncate_file_tail "${PROJECT_DIR}/logs/betting_restart.log" 20 2

    find "$PROJECT_DIR" -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
    find "$PROJECT_DIR" -maxdepth 1 -type f -name 'debug_*.png' -mtime +2 -delete 2>/dev/null || true
    find "$PROJECT_DIR/logs" -maxdepth 1 -type f -name 'debug_*.html' -mtime +2 -delete 2>/dev/null || true

    find /tmp -maxdepth 1 -type f -name '*.lock' -mmin +120 -user "$(whoami)" -print -delete 2>/dev/null | while read -r lock; do
        log "removed stale lock: $lock"
    done

    vacuum_journal_if_needed "$use_pct"

    if [[ "${CLEANUP_STOP_SERVICE:-0}" == "1" ]]; then
        log "restarting betting-arb"
        sudo systemctl start betting-arb || true
    fi

    use_pct=$(df / | awk 'NR==2 {gsub(/%/,"",$5); print $5}')
    log_health_snapshot "$use_pct"
    log "disk after:  $(df -h / | awk 'NR==2 {print $3 " used / " $2 " total (" $5 " full)"}')"
    log "=== maintenance end ==="
}

main "$@"
