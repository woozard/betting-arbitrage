#!/usr/bin/env bash
# Place a one-off MLB moneyline test bet on LowVig from EC2.
set -euo pipefail
cd "$(dirname "$0")/.."
source venv/bin/activate
export SKIP_DB_BOOTSTRAP=1

TEAM_NAME="${1:-Miami Marlins}"
STAKE="${2:-4}"

PYTHONPATH=. python test_lowvig_place_bet.py --team-name "$TEAM_NAME" --stake "$STAKE"
