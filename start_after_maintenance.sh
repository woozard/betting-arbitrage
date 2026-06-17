#!/bin/bash
# Run this on EC2 after your deploy/maintenance is complete.

set -e

echo "=== Starting Betting Arbitrage after Maintenance ==="

cd ~/betting-arbitrage

echo "1. Reloading systemd (in case you edited the service file)..."
sudo systemctl daemon-reload

echo "2. Starting the service..."
sudo systemctl start betting-arb

echo "3. Status:"
sudo systemctl status betting-arb --no-pager -l

echo ""
echo "The scheduler should now be running persistently again."
echo ""
echo "Monitor with:"
echo "  sudo journalctl -u betting-arb -f"
echo "  tail -f logs/sports411_betting.log"
echo "  tail -f logs/betamapola_betting.log"
echo "  tail -f logs/betwar_betting.log"
echo "  tail -f logs/arbitrage.log | grep -E 'Arbs:|Close Arb|Bet'"
