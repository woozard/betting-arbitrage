#!/bin/bash
# Place a manual S411 moneyline test bet FROM EC2 ONLY (attach Chrome + open-bets confirm).
# Usage:
#   ./scripts/place_s411_test_bet_ec2.sh --list-only
#   ./scripts/place_s411_test_bet_ec2.sh --team-name "Miami Marlins" --odds "-148" --stake 4
set -euo pipefail
cd "$(dirname "$0")/.."
export SKIP_DB_BOOTSTRAP=1
export SPORTS411_XDOTOOL_BET_ONLY=1

if ! command -v xvfb-run >/dev/null 2>&1; then
  echo "xvfb-run required: sudo apt-get install -y xvfb"
  exit 1
fi

exec xvfb-run -a venv/bin/python test_s411_place_bet.py --attach --no-proxy "$@"
