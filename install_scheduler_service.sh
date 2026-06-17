#!/bin/bash
# Run this ON EC2 as the ubuntu user (or with sudo where needed)
# It sets up the scheduler as a systemd service so it survives
# laptop lid close, SSH disconnects, reboots, etc.

set -e

echo "=== Betting Arbitrage Scheduler - Systemd Service Installer ==="

cd ~/betting-arbitrage

# Make sure we're in venv context for any python calls if needed
if [ -z "$VIRTUAL_ENV" ]; then
    echo "Activating venv..."
    source venv/bin/activate
fi

echo "1. Stopping any manually running scheduler..."
pkill -f 'python.*scheduler.py' || true
pkill -f 'flock' || true
sleep 2

echo "2. Installing service file..."
sudo cp betting-arb.service /etc/systemd/system/betting-arb.service
sudo systemctl daemon-reload

echo "3. Enabling and starting the service..."
sudo systemctl enable betting-arb.service
sudo systemctl restart betting-arb.service

echo "4. Status:"
sudo systemctl status betting-arb.service --no-pager -l

echo ""
echo "=== Useful commands ==="
echo "  sudo systemctl status betting-arb"
echo "  sudo journalctl -u betting-arb -f          # live logs"
echo "  sudo journalctl -u betting-arb --since '1 hour ago'"
echo "  sudo systemctl stop betting-arb            # stop for deploy/maintenance"
echo "  sudo systemctl start betting-arb"
echo "  sudo systemctl restart betting-arb"
echo ""
echo "For temporary stops (e.g. deploys):"
echo "  bash stop_for_maintenance.sh"
echo "  # ... do your deploy/rsync/changes ..."
echo "  bash start_after_maintenance.sh"
echo ""
echo "The scheduler will now start automatically on boot and survive SSH disconnects / laptop lid closes."
echo ""
echo "To see the betting job logs (still go to files):"
echo "  tail -f logs/sports411_betting.log"
echo "  tail -f logs/betamapola_betting.log"
echo "  tail -f logs/betwar_betting.log"
echo "  tail -f logs/betwar_odds.log"
echo "  tail -f logs/arbitrage.log"
