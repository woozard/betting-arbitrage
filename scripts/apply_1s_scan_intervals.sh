#!/usr/bin/env bash
# Set 1s odds-poll / idle-scan intervals for 4casters + S411 and fastest backup arb scan.
set -euo pipefail

APP_DIR="${1:-/home/ubuntu/betting-arbitrage}"
ENV_FILE="$APP_DIR/.env"

if [[ ! -f "$ENV_FILE" ]]; then
  echo "Missing $ENV_FILE" >&2
  exit 1
fi

set_kv() {
  local key="$1"
  local value="$2"
  if grep -q "^${key}=" "$ENV_FILE"; then
    sed -i "s/^${key}=.*/${key}=${value}/" "$ENV_FILE"
  else
    echo "${key}=${value}" >> "$ENV_FILE"
  fi
}

# Book idle odds (inline arb detection path).
set_kv ODDS_WATCH_POLL_SEC 1
set_kv ODDS_WATCH_FORCE_SCAN_SEC 1
set_kv BETTING_IDLE_POLL_SEC 1
set_kv FOURCASTERS_ODDS_POLL_SEC 1
set_kv FOURCASTERS_ODDS_FORCE_SCAN_SEC 1
set_kv FOURCASTERS_ODDS_IDLE_POLL_SEC 1
set_kv SPORTS411_ODDS_POLL_SEC 1
set_kv SPORTS411_ODDS_FORCE_SCAN_SEC 1
# Backup DB arb scanner: no extra sleep between passes (each pass still ~2–3s).
set_kv ARB_SCAN_DELAY_SEC 0

echo "=== Scan interval env ==="
grep -E '^(ODDS_WATCH_|BETTING_IDLE_POLL|FOURCASTERS_ODDS_|SPORTS411_ODDS_|ARB_SCAN_DELAY)' "$ENV_FILE" || true

sudo systemctl restart betting-arb
sleep 2
sudo systemctl is-active betting-arb
