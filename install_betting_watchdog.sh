#!/bin/bash
# Install daily betting restart + log-staleness health checks on EC2.
set -euo pipefail

PROJECT_DIR="${PROJECT_DIR:-$HOME/betting-arbitrage}"
cd "$PROJECT_DIR"

chmod +x restart_betting_jobs.sh check_betting_health.sh

echo "=== Installing betting restart + healthcheck timers ==="
sudo cp betting-arb-betting-restart.service betting-arb-betting-restart.timer /etc/systemd/system/
sudo cp betting-arb-healthcheck.service betting-arb-healthcheck.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now betting-arb-betting-restart.timer
sudo systemctl enable --now betting-arb-healthcheck.timer

echo ""
echo "Timers:"
systemctl list-timers 'betting-arb-betting*' 'betting-arb-health*' --no-pager

echo ""
echo "Run healthcheck once now:"
./check_betting_health.sh
tail -5 logs/betting_healthcheck.log

echo ""
echo "Monitor:"
echo "  tail -f logs/betting_healthcheck.log"
echo "  tail -f logs/betting_restart.log"