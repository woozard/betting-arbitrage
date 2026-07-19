#!/usr/bin/env bash
# Set MIN_ARB_PROFIT_PCT on base + stack env overlays and restart arb scanners.
# Usage: scripts/apply_ml_threshold.sh [1.00] [/home/ubuntu/betting-arbitrage]
set -euo pipefail

THRESHOLD="${1:-1.00}"
APP_DIR="${2:-/home/ubuntu/betting-arbitrage}"

if [[ ! -d "$APP_DIR" ]]; then
  echo "Missing app dir: $APP_DIR" >&2
  exit 1
fi

updated=0
for f in "$APP_DIR/.env" "$APP_DIR/.env.wnba" "$APP_DIR/.env.mlb"; do
  [[ -f "$f" ]] || continue
  if grep -q '^MIN_ARB_PROFIT_PCT=' "$f"; then
    sed -i "s/^MIN_ARB_PROFIT_PCT=.*/MIN_ARB_PROFIT_PCT=${THRESHOLD}/" "$f"
  else
    echo "MIN_ARB_PROFIT_PCT=${THRESHOLD}" >> "$f"
  fi
  # Drop explicit override so MIN_ARB_PROFIT_PCT drives ARB_MAX_TOTAL_PROB.
  sed -i '/^ARB_MAX_TOTAL_PROB=/d' "$f"
  echo "Updated $f:"
  grep '^MIN_ARB_PROFIT_PCT=' "$f"
  updated=$((updated + 1))
done

if [[ "$updated" -eq 0 ]]; then
  echo "No env files found under $APP_DIR" >&2
  exit 1
fi

# Restart only arb scanners so they pick up the new threshold.
pkill -f 'run_stack_job.sh wnba arbitrage.py' || true
pkill -f 'run_stack_job.sh mlb arbitrage.py' || true
pkill -f 'python3.*arbitrage\.py' || true
sleep 2
rm -f /tmp/arbitrage_wnba.lock /tmp/arbitrage_mlb.lock /tmp/arbitrage.lock
echo "Arb scanners killed; scheduler will respawn with MIN_ARB_PROFIT_PCT=${THRESHOLD}"
