#!/bin/bash
# Run this on EC2 when you want to stop the scheduler for a deploy, code changes, etc.
# It stops the systemd service cleanly and kills any lingering processes.

set -e

echo "=== Stopping Betting Arbitrage for Maintenance ==="

echo "1. Stopping the systemd service..."
sudo systemctl stop betting-arb || true

echo "2. Killing any remaining scheduler or child processes..."
pkill -f 'python.*scheduler.py' || true
pkill -f 'flock' || true

# The betting jobs use Selenium, which can leave orphaned chrome processes
echo "3. Cleaning up potential Selenium/chrome orphans..."
pkill -f 'chromedriver' || true
pkill -f 'google-chrome' || true

sleep 2

echo "4. Final check:"
ps aux | grep -E 'scheduler|flock|betting' | grep -v grep || echo "No related processes found. Good."

echo ""
echo "=== Service is now stopped. ==="
sudo systemctl status betting-arb --no-pager -l || true

echo ""
echo "You can now safely deploy, edit code, rsync, etc."
echo "When ready to resume:"
echo "  sudo systemctl start betting-arb"
echo "  # or"
echo "  sudo systemctl restart betting-arb"
