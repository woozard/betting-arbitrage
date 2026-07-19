#!/usr/bin/env bash
# Run a betting/arbitrage script under a named stack env
# (.env.wnba / .env.mlb / .env.ufc).
#
# Usage:
#   scripts/run_stack_job.sh wnba sports411_betting.py
#   scripts/run_stack_job.sh mlb  arbitrage.py
#   scripts/run_stack_job.sh ufc  arbitrage.py
set -euo pipefail

STACK="${1:-}"
SCRIPT="${2:-}"
if [[ -z "$STACK" || -z "$SCRIPT" ]]; then
  echo "Usage: $0 <stack> <script.py>" >&2
  exit 2
fi

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

STACK_ENV="$ROOT/.env.${STACK}"
BASE_ENV="$ROOT/.env"

if [[ ! -f "$STACK_ENV" ]]; then
  echo "Missing stack env: $STACK_ENV" >&2
  exit 1
fi

# Shared base first, then stack overrides (accounts/sport/stakes/redis db).
set -a
# shellcheck disable=SC1090
[[ -f "$BASE_ENV" ]] && . "$BASE_ENV"
# shellcheck disable=SC1090
. "$STACK_ENV"
set +a

export STACK_NAME="$STACK"
export ARB_STACK="$STACK"
export PYTHONUNBUFFERED=1

SCRIPT_PATH="$SCRIPT"
if [[ ! -f "$SCRIPT_PATH" ]]; then
  SCRIPT_PATH="$ROOT/$SCRIPT"
fi
if [[ ! -f "$SCRIPT_PATH" ]]; then
  echo "Script not found: $SCRIPT" >&2
  exit 1
fi

exec "$ROOT/venv/bin/python3" "$SCRIPT_PATH"
