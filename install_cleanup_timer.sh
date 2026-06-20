#!/bin/bash
# Install periodic server maintenance on EC2 (systemd timer).
# Run from ~/betting-arbitrage after deploy.

set -euo pipefail

PROJECT_DIR="${PROJECT_DIR:-$HOME/betting-arbitrage}"
cd "$PROJECT_DIR"

chmod +x cleanup_disk.sh check_betting_health.sh restart_betting_jobs.sh 2>/dev/null || true
chmod +x cleanup_disk.sh

if [[ ! -x cleanup_disk.sh ]]; then
    echo "ERROR: cleanup_disk.sh is not executable after chmod" >&2
    exit 1
fi

echo "=== Installing betting-arb maintenance timer (hourly) ==="
sudo cp betting-arb-cleanup.service /etc/systemd/system/
sudo cp betting-arb-cleanup.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now betting-arb-cleanup.timer
sudo systemctl restart betting-arb-cleanup.timer

echo ""
echo "Timer status:"
systemctl list-timers betting-arb-cleanup.timer --no-pager

echo ""
echo "Running maintenance once now..."
./cleanup_disk.sh

echo ""
echo "Verify timer + logs:"
echo "  systemctl status betting-arb-cleanup.timer"
echo "  tail -f logs/cleanup_disk.log"
echo "  journalctl -u betting-arb-cleanup.service -n 20 --no-pager"
