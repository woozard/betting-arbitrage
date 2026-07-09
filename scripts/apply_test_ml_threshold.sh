#!/usr/bin/env bash
# Set ML arb threshold to -1.02% for end-to-end testing (allows near-miss arbs).
set -euo pipefail

APP_DIR="${1:-/home/ubuntu/betting-arbitrage}"
ENV_FILE="$APP_DIR/.env"

if [[ ! -f "$ENV_FILE" ]]; then
  echo "Missing $ENV_FILE" >&2
  exit 1
fi

if grep -q '^MIN_ARB_PROFIT_PCT=' "$ENV_FILE"; then
  sed -i 's/^MIN_ARB_PROFIT_PCT=.*/MIN_ARB_PROFIT_PCT=-1.02/' "$ENV_FILE"
else
  echo 'MIN_ARB_PROFIT_PCT=-1.02' >> "$ENV_FILE"
fi

# Drop explicit override so MIN_ARB_PROFIT_PCT drives ARB_MAX_TOTAL_PROB.
sed -i '/^ARB_MAX_TOTAL_PROB=/d' "$ENV_FILE"

echo "Updated MIN_ARB_PROFIT_PCT:"
grep '^MIN_ARB_PROFIT_PCT=' "$ENV_FILE"

sudo systemctl restart betting-arb
sleep 2
sudo systemctl is-active betting-arb
