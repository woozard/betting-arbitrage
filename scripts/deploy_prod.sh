#!/usr/bin/env bash
# Deploy betting-arbitrage to prod EC2 and apply test ML threshold.
set -euo pipefail

HOST="${DEPLOY_HOST:-ubuntu@100.53.194.189}"
APP_DIR="${DEPLOY_APP_DIR:-/home/ubuntu/betting-arbitrage}"
SSH_KEY="${SSH_KEY:-${DEPLOY_SSH_KEY:-}}"

if [[ -z "$SSH_KEY" || ! -f "$SSH_KEY" ]]; then
  echo "Set SSH_KEY to your EC2 PEM path, e.g.:" >&2
  echo '  SSH_KEY="/path/to/pinn-arb-new (1).pem" bash scripts/deploy_prod.sh' >&2
  exit 1
fi

chmod 600 "$SSH_KEY"
SSH_OPTS=(-i "$SSH_KEY" -o StrictHostKeyChecking=accept-new)

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
echo "=== Rsync to $HOST:$APP_DIR ==="
rsync -avz --delete \
  --exclude '.git' \
  --exclude '__pycache__' \
  --exclude '.env' \
  --exclude 'venv' \
  --exclude 'logs' \
  --exclude '.DS_Store' \
  -e "ssh ${SSH_OPTS[*]}" \
  "$ROOT/" "$HOST:$APP_DIR/"

echo "=== Apply ML threshold + restart ==="
ssh "${SSH_OPTS[@]}" "$HOST" "bash $APP_DIR/scripts/apply_test_ml_threshold.sh $APP_DIR"

echo "=== Verify threshold in process env ==="
ssh "${SSH_OPTS[@]}" "$HOST" "grep '^MIN_ARB_PROFIT_PCT=' $APP_DIR/.env && sudo systemctl is-active betting-arb"

echo "=== Done ==="
