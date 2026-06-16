#!/bin/bash
# Kill only the long-running betting Chrome jobs; scheduler respawns them on next tick.
set -euo pipefail

PROJECT_DIR="${PROJECT_DIR:-$HOME/betting-arbitrage}"
LOG_DIR="${LOG_DIR:-$PROJECT_DIR/logs}"
STAMP="$(date -u '+%Y-%m-%d %H:%M:%S UTC')"
LOG_FILE="$LOG_DIR/betting_restart.log"

mkdir -p "$LOG_DIR"
{
  echo "[$STAMP] === Restarting betting jobs ==="

  for pattern in sports411_betting.py betamapola_betting.py paradisewager_betting.py; do
    pkill -f "$pattern" || true
  done

  sleep 2
  pkill -9 -f 'sports411_betting.py|betamapola_betting.py|paradisewager_betting.py' || true

  rm -f /tmp/sports411_betting.lock /tmp/betamapola_betting.lock /tmp/paradisewager_betting.lock

  echo "[$STAMP] Betting jobs killed; scheduler will respawn within ~30s"
} >>"$LOG_FILE" 2>&1