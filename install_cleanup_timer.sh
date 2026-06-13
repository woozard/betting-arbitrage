#!/bin/bash
# Install periodic disk cleanup on EC2 (systemd timer).
# Run from ~/betting-arbitrage after rsync/deploy.

set -euo pipefail

PROJECT_DIR="${PROJECT_DIR:-$HOME/betting-arbitrage}"
cd "$PROJECT_DIR"

chmod +x cleanup_disk.sh

echo "=== Installing betting-arb cleanup timer ==="
sudo cp betting-arb-cleanup.service /etc/systemd/system/
sudo cp betting-arb-cleanup.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now betting-arb-cleanup.timer

echo ""
echo "Timer status:"
systemctl list-timers betting-arb-cleanup.timer --no-pager

echo ""
echo "Run once now to verify:"
./cleanup_disk.sh

echo ""
echo "Monitor future runs:"
echo "  tail -f logs/cleanup_disk.log"
echo "  journalctl -u betting-arb-cleanup.service -n 50 --no-pager"