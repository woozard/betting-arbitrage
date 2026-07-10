#!/usr/bin/env bash
# One-shot deploy of betting-arbitrage to prod EC2.
#
# Ships the current working tree (excluding .git/.env/venv/logs) to the prod
# host over SSH, restarts the betting-arb service, and verifies it came back up.
#
# Usage:
#   bash scripts/deploy_prod.sh                # deploy + restart + verify
#   bash scripts/deploy_prod.sh --push         # also `git push origin master` first
#   bash scripts/deploy_prod.sh --tests        # run pytest before deploying
#   bash scripts/deploy_prod.sh --push --tests # both
#
# Env overrides:
#   SSH_KEY       path to EC2 PEM (required; has a sensible default below)
#   DEPLOY_HOST   ssh target (default ubuntu@ec2-100-53-194-189.compute-1.amazonaws.com)
#   DEPLOY_APP_DIR remote app dir (default /home/ubuntu/betting-arbitrage)
#   DEPLOY_SERVICE systemd unit (default betting-arb)
set -euo pipefail

HOST="${DEPLOY_HOST:-ubuntu@ec2-100-53-194-189.compute-1.amazonaws.com}"
APP_DIR="${DEPLOY_APP_DIR:-/home/ubuntu/betting-arbitrage}"
SERVICE="${DEPLOY_SERVICE:-betting-arb}"
SSH_KEY="${SSH_KEY:-${DEPLOY_SSH_KEY:-/home/ubuntu/.cursor/projects/workspace/uploads/pinn-arb-new_d7f4.pem}}"

DO_PUSH=0
DO_TESTS=0
for arg in "$@"; do
  case "$arg" in
    --push) DO_PUSH=1 ;;
    --tests) DO_TESTS=1 ;;
    *) echo "Unknown arg: $arg" >&2; exit 2 ;;
  esac
done

if [[ -z "$SSH_KEY" || ! -f "$SSH_KEY" ]]; then
  echo "SSH_KEY not found at '$SSH_KEY'. Set SSH_KEY=/path/to/prod.pem" >&2
  exit 1
fi
chmod 600 "$SSH_KEY" 2>/dev/null || true
SSH_OPTS=(-i "$SSH_KEY" -o StrictHostKeyChecking=accept-new)

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

if [[ "$DO_TESTS" == "1" ]]; then
  echo "=== Running tests ==="
  SKIP_DB_BOOTSTRAP=1 DB_USERNAME=u DB_PASSWORD=p DB_HOST=localhost DB_PORT=3306 \
    DB_DATABASE=test REDIS_HOST=localhost REDIS_PORT=6379 \
    python3 -m pytest tests/ -q
fi

if [[ "$DO_PUSH" == "1" ]]; then
  echo "=== git push origin master ==="
  git push origin master
fi

echo "=== Shipping working tree to $HOST:$APP_DIR ==="
tar czf - \
  --exclude='.git' \
  --exclude='.env' \
  --exclude='venv' \
  --exclude='logs' \
  --exclude='__pycache__' \
  --exclude='.DS_Store' \
  -C "$ROOT" . \
  | ssh "${SSH_OPTS[@]}" "$HOST" "cd '$APP_DIR' && tar xzf -"

echo "=== Restarting $SERVICE ==="
ssh "${SSH_OPTS[@]}" "$HOST" "sudo systemctl restart '$SERVICE' && sleep 2 && systemctl is-active '$SERVICE'"

echo "=== Verify (service + key flags + deployed HEAD) ==="
LOCAL_HEAD="$(git rev-parse --short HEAD 2>/dev/null || echo unknown)"
echo "local HEAD: $LOCAL_HEAD"
ssh "${SSH_OPTS[@]}" "$HOST" "
  echo -n 'service: '; systemctl is-active '$SERVICE'
  echo '--- .env flags ---'
  grep -E '^(BET_STAKE|MIN_ARB_PROFIT_PCT|FOURCASTERS_FAST_PLACE|S411_FAST_PLACE|FOURCASTERS_RETRY_SLEEP_SEC|ODDS_WATCH_POLL_SEC|ARB_SCAN_DELAY_SEC|PARALLEL_EXCHANGE_ARB_BETTING|ACTIVE_ARB_BOOK_PAIRS)=' '$APP_DIR/.env' || true
  echo '--- code sentinels (should be >0) ---'
  echo -n 'fast_place_moneyline: '; grep -c '_fast_place_moneyline' '$APP_DIR/controllers/FourCastersController.py' || true
  echo -n 'S411_FAST_PLACE: '; grep -c 'S411_FAST_PLACE' '$APP_DIR/controllers/Sports411Controller.py' || true
"

echo "=== Deploy complete ==="
