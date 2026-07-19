#!/bin/bash
# Periodic server maintenance for betting-arbitrage on EC2.
#
# Safe to run while betting-arb is running:
#   - kills Chrome/chromedriver orphans not owned by a live *_betting.py / *_odds.py job
#   - removes tmp profile dirs not used by live jobs (age/cap based)
# Does NOT stop the scheduler by default.
#
# Usage:
#   ./cleanup_disk.sh              # normal run (includes orphan chrome cleanup)
#   CLEANUP_ORPHAN_CHROME_ONLY=1 ./cleanup_disk.sh  # light pass (health timer)
#   CLEANUP_STOP_SERVICE=1 ./cleanup_disk.sh   # stop scheduler, kill all chrome, restart
#   CLEANUP_KILL_ORPHAN_CHROME=0 ./cleanup_disk.sh  # skip orphan process kill
#
# Environment overrides (optional):
#   PROJECT_DIR, MIN_AGE_MINUTES, MAX_TMP_PROFILES, LOG_ROTATED_AGE_DAYS,
#   LOG_DATED_AGE_DAYS, DEBUG_MAX_AGE_HOURS, DEBUG_MAX_MB,
#   SCHEDULER_LOG_MAX_MB, KEEP_SCHEDULER_LOG_MB, JOURNAL_MAX_MB, DISK_WARN_PCT,
#   CLEANUP_STOP_SERVICE, CLEANUP_ORPHAN_CHROME_ONLY, CLEANUP_KILL_ORPHAN_CHROME,
#   ORPHAN_CHROME_MIN_AGE_SEC

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
CLEANUP_KILL_ORPHAN_CHROME="${CLEANUP_KILL_ORPHAN_CHROME:-1}"
ORPHAN_CHROME_MIN_AGE_SEC="${ORPHAN_CHROME_MIN_AGE_SEC:-90}"
CLEANUP_LOG="${PROJECT_DIR}/logs/cleanup_disk.log"

# Manual VNC / seeded profiles — never kill these.
PROTECTED_PROFILE_RE='\.sports411-chrome-profile|SPORTS411_CHROME_USER_DATA'

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

count_chrome_procs() {
    # Count chrome + chromedriver processes (exclude this script / grep).
    ps -eo pid=,args= 2>/dev/null \
        | grep -E '(chrome|chromedriver|chromium)' \
        | grep -vE '(cleanup_disk|grep)' \
        | wc -l \
        | tr -d ' '
}

collect_live_job_pids() {
    # Python book jobs that own Selenium browsers.
    pgrep -f '(_betting\.py|_odds\.py)' 2>/dev/null || true
}

pid_owned_by_live_job() {
    # Walk PPID chain; true if this process descends from a live betting/odds job.
    local pid="$1"
    local jobs="$2"
    local guard=0
    local ppid

    [[ -z "$pid" || "$pid" -le 1 ]] && return 1
    [[ -z "$jobs" ]] && return 1

    while [[ "$pid" -gt 1 && "$guard" -lt 40 ]]; do
        if printf '%s\n' "$jobs" | grep -qxF "$pid"; then
            return 0
        fi
        ppid="$(ps -o ppid= -p "$pid" 2>/dev/null | tr -d ' ' || true)"
        [[ -z "$ppid" || "$ppid" == "$pid" ]] && break
        pid="$ppid"
        guard=$((guard + 1))
    done
    return 1
}

collect_active_tmp_dirs() {
    # Profiles owned by live book jobs only (not orphan chrome — that was the leak).
    local jobs active=()
    local pid cmdline path
    jobs="$(collect_live_job_pids)"
    [[ -z "$jobs" ]] && return 0

    while IFS= read -r pid; do
        [[ -z "$pid" ]] && continue
        # Job PID itself rarely has the profile path; scan its process group descendants
        # via all chrome/chromedriver that descend from this job.
        :
    done <<< "$jobs"

    while IFS= read -r line; do
        pid="$(echo "$line" | awk '{print $1}')"
        cmdline="$(echo "$line" | cut -d' ' -f2-)"
        [[ -z "$pid" ]] && continue
        echo "$cmdline" | grep -qE "$PROTECTED_PROFILE_RE" && continue
        if ! pid_owned_by_live_job "$pid" "$jobs"; then
            continue
        fi
        for path in $(echo "$cmdline" | grep -oE "${PROJECT_DIR}/tmp/(chrome_user_data_[^ ]+|brightdata_proxy_[^ ]+|fourcasters_chrome_[^ ]+)" || true); do
            active+=("$path")
        done
    done < <(ps -eo pid=,args= 2>/dev/null | grep -E '(chrome|chromedriver|chromium)' | grep -v grep || true)

    # Also catch profile paths that appear on the python job cmdline (rare).
    while IFS= read -r pid; do
        [[ -z "$pid" ]] && continue
        cmdline="$(ps -o args= -p "$pid" 2>/dev/null || true)"
        for path in $(echo "$cmdline" | grep -oE "${PROJECT_DIR}/tmp/(chrome_user_data_[^ ]+|brightdata_proxy_[^ ]+|fourcasters_chrome_[^ ]+)" || true); do
            active+=("$path")
        done
    done <<< "$jobs"

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

proc_etime_seconds() {
    # Parse ps etime ([[dd-]hh:]mm:ss) → seconds.
    local etime="$1"
    local days=0 hours=0 mins=0 secs=0
    if [[ "$etime" == *-* ]]; then
        days="${etime%%-*}"
        etime="${etime#*-}"
    fi
    local IFS=':'
    # shellcheck disable=SC2086
    set -- $etime
    if [[ $# -eq 3 ]]; then
        hours=$1; mins=$2; secs=$3
    elif [[ $# -eq 2 ]]; then
        mins=$1; secs=$2
    else
        secs=$1
    fi
    echo $((10#$days * 86400 + 10#$hours * 3600 + 10#$mins * 60 + 10#$secs))
}

cleanup_orphan_chrome_procs() {
    local before after killed=0 skipped_young=0 skipped_live=0 skipped_protected=0
    local jobs pid etime age cmdline

    before="$(count_chrome_procs)"
    jobs="$(collect_live_job_pids)"
    job_count=0
    if [[ -n "$jobs" ]]; then
        job_count="$(printf '%s\n' "$jobs" | grep -c . || true)"
    fi

    log "chrome before orphan cleanup: ${before} (live book jobs: ${job_count})"

    while IFS= read -r line; do
        [[ -z "$line" ]] && continue
        pid="$(echo "$line" | awk '{print $1}')"
        etime="$(echo "$line" | awk '{print $2}')"
        cmdline="$(echo "$line" | cut -d' ' -f3-)"
        [[ -z "$pid" ]] && continue

        # Only touch browsers tied to this project's Selenium tmp profiles,
        # or chromedriver binaries (always Selenium). Never touch VNC/manual profiles.
        if echo "$cmdline" | grep -qE "$PROTECTED_PROFILE_RE"; then
            skipped_protected=$((skipped_protected + 1))
            continue
        fi

        local is_driver=0 is_our_profile=0
        echo "$cmdline" | grep -qiE 'chromedriver' && is_driver=1
        echo "$cmdline" | grep -qE "${PROJECT_DIR}/tmp/(chrome_user_data_|brightdata_proxy_|fourcasters_chrome_)" && is_our_profile=1

        if (( is_driver == 0 && is_our_profile == 0 )); then
            continue
        fi

        if pid_owned_by_live_job "$pid" "$jobs"; then
            skipped_live=$((skipped_live + 1))
            continue
        fi

        age="$(proc_etime_seconds "$etime")"
        if (( age < ORPHAN_CHROME_MIN_AGE_SEC )); then
            skipped_young=$((skipped_young + 1))
            continue
        fi

        kill "$pid" 2>/dev/null || true
        killed=$((killed + 1))
    done < <(
        ps -eo pid=,etime=,args= 2>/dev/null \
            | grep -E '(chrome|chromedriver|chromium)' \
            | grep -vE '(cleanup_disk|grep)' \
            || true
    )

    sleep 1
    # Escalate leftovers that still match our tmp profiles / chromedriver.
    while IFS= read -r line; do
        pid="$(echo "$line" | awk '{print $1}')"
        cmdline="$(echo "$line" | cut -d' ' -f2-)"
        [[ -z "$pid" ]] && continue
        echo "$cmdline" | grep -qE "$PROTECTED_PROFILE_RE" && continue
        if pid_owned_by_live_job "$pid" "$jobs"; then
            continue
        fi
        if echo "$cmdline" | grep -qiE 'chromedriver' \
            || echo "$cmdline" | grep -qE "${PROJECT_DIR}/tmp/(chrome_user_data_|brightdata_proxy_|fourcasters_chrome_)"; then
            kill -9 "$pid" 2>/dev/null || true
        fi
    done < <(
        ps -eo pid=,args= 2>/dev/null \
            | grep -E '(chrome|chromedriver|chromium)' \
            | grep -vE '(cleanup_disk|grep)' \
            || true
    )

    after="$(count_chrome_procs)"
    log "orphan chrome kill: signalled=${killed}, live_kept=${skipped_live}, young_kept=${skipped_young}, protected_kept=${skipped_protected}; chrome after=${after}"
}

cleanup_tmp_profiles() {
    local removed=0
    local min_age="$MIN_AGE_MINUTES"
    local -a active_dirs=()
    local -a all_dirs=()

    mapfile -t active_dirs < <(collect_active_tmp_dirs || true)

    shopt -s nullglob
    for pat in tmp/chrome_user_data_* tmp/brightdata_proxy_* tmp/fourcasters_chrome_*; do
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

    local active_set=""
    if ((${#active_dirs[@]})); then
        active_set="$(printf '%s\n' "${active_dirs[@]}")"
    fi

    local idx=0
    for d in "${all_dirs[@]}"; do
        idx=$((idx + 1))
        local reason=""

        if [[ -n "$active_set" ]] && printf '%s\n' "$active_set" | grep -qxF "$d"; then
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

    log "removed ${removed} tmp profile dir(s); ${#all_dirs[@]} total before cleanup; ${#active_dirs[@]} live-owned kept"
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
    local svc mem avail chrome_n
    svc="$(systemctl is-active betting-arb 2>/dev/null || echo unknown)"
    mem="$(free -h | awk '/^Mem:/ {print $3 "/" $2 " used, " $7 " avail"}')"
    avail="$(df -h / | awk 'NR==2 {print $4 " free (" $5 " used)"}')"
    chrome_n="$(count_chrome_procs)"
    log "health: betting-arb=${svc} | chrome=${chrome_n} | mem=${mem} | disk=${avail}"
    if (( use_pct >= DISK_WARN_PCT )); then
        log "WARN: root filesystem at ${use_pct}% — review logs/tmp growth"
    fi
    if (( chrome_n > 80 )); then
        log "WARN: chrome process count high (${chrome_n}) — orphan leak likely"
    fi
}

main() {
    cd "$PROJECT_DIR"
    mkdir -p logs/debug tmp

    # Light path used by the healthcheck timer (~every 10 minutes).
    if [[ "${CLEANUP_ORPHAN_CHROME_ONLY:-0}" == "1" ]]; then
        log "=== orphan chrome cleanup start ==="
        log "chrome before: $(count_chrome_procs)"
        cleanup_orphan_chrome_procs
        # Drop inactive profiles quickly after orphans are killed.
        MIN_AGE_MINUTES=2 cleanup_tmp_profiles
        log "chrome after: $(count_chrome_procs)"
        log "=== orphan chrome cleanup end ==="
        return 0
    fi

    log "=== maintenance start (min_age=${MIN_AGE_MINUTES}m, max_profiles=${MAX_TMP_PROFILES}, orphan_chrome=${CLEANUP_KILL_ORPHAN_CHROME}) ==="
    log "disk before: $(df -h / | awk 'NR==2 {print $3 " used / " $2 " total (" $5 " full)"}')"
    log "chrome before: $(count_chrome_procs)"

    local use_pct
    use_pct=$(df / | awk 'NR==2 {gsub(/%/,"",$5); print $5}')

    if [[ "${CLEANUP_STOP_SERVICE:-0}" == "1" ]]; then
        log "stopping betting-arb (CLEANUP_STOP_SERVICE=1)"
        sudo systemctl stop betting-arb || true
        sleep 3
        pkill -f 'chromedriver' 2>/dev/null || true
        pkill -f 'google-chrome' 2>/dev/null || true
        pkill -f 'chromium' 2>/dev/null || true
        sleep 2
        log "chrome after full kill: $(count_chrome_procs)"
    elif [[ "$CLEANUP_KILL_ORPHAN_CHROME" == "1" ]]; then
        cleanup_orphan_chrome_procs
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
