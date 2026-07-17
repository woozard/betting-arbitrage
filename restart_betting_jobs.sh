#!/bin/bash
# Kill long-running betting Chrome jobs for both stacks; scheduler respawns them.
set -euo pipefail

PROJECT_DIR="${PROJECT_DIR:-$HOME/betting-arbitrage}"
LOG_DIR="${LOG_DIR:-$PROJECT_DIR/logs}"
STAMP="$(date -u '+%Y-%m-%d %H:%M:%S UTC')"
LOG_FILE="$LOG_DIR/betting_restart.log"

mkdir -p "$LOG_DIR"
{
  echo "[$STAMP] === Restarting betting jobs (wnba+mlb+ufc) ==="

  for pattern in \
    sports411_betting.py \
    betamapola_betting.py \
    fourcasters_betting.py \
    ps3838_betting.py \
    paradisewager_betting.py \
    betwar_betting.py \
    arbitrage.py
  do
    pkill -f "$pattern" || true
  done

  sleep 2
  pkill -9 -f 'sports411_betting.py|betamapola_betting.py|fourcasters_betting.py|ps3838_betting.py|paradisewager_betting.py|betwar_betting.py|arbitrage.py' || true

  rm -f \
    /tmp/sports411_betting.lock \
    /tmp/sports411_betting_wnba.lock \
    /tmp/sports411_betting_mlb.lock \
    /tmp/sports411_betting_ufc.lock \
    /tmp/betamapola_betting.lock \
    /tmp/betamapola_betting_wnba.lock \
    /tmp/betamapola_betting_mlb.lock \
    /tmp/betamapola_betting_ufc.lock \
    /tmp/fourcasters_betting.lock \
    /tmp/fourcasters_betting_wnba.lock \
    /tmp/fourcasters_betting_mlb.lock \
    /tmp/fourcasters_betting_ufc.lock \
    /tmp/ps3838_betting.lock \
    /tmp/ps3838_betting_mlb.lock \
    /tmp/arbitrage.lock \
    /tmp/arbitrage_wnba.lock \
    /tmp/arbitrage_mlb.lock \
    /tmp/arbitrage_ufc.lock \
    /tmp/paradisewager_betting.lock \
    /tmp/betwar_betting.lock

  echo "[$STAMP] Betting jobs killed; scheduler will respawn within ~30s"
} >>"$LOG_FILE" 2>&1
