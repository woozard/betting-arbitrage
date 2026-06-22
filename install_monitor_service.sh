#!/bin/bash
# Install the monitoring dashboard as a systemd service.
set -euo pipefail

cd ~/betting-arbitrage
source venv/bin/activate

sudo cp betting-arb-monitor.service /etc/systemd/system/betting-arb-monitor.service
sudo systemctl daemon-reload
sudo systemctl enable betting-arb-monitor.service
sudo systemctl restart betting-arb-monitor.service

echo ""
PUBLIC_IP=$(curl -s --connect-timeout 2 http://169.254.169.254/latest/meta-data/public-ipv4 || true)
echo "Monitor dashboard (on server): http://127.0.0.1:8080/"
if [[ -n "$PUBLIC_IP" ]]; then
  echo "Public URL (after opening AWS SG port 8080): http://${PUBLIC_IP}:8080/"
fi
echo ""
echo "SSH tunnel from your laptop:"
echo "  ssh -L 8080:127.0.0.1:8080 -i YOUR.pem ubuntu@${PUBLIC_IP:-EC2_IP}"
echo "  then open http://localhost:8080/"
echo ""
echo "Optional: set MONITOR_TOKEN in .env for auth (?token=... or X-Monitor-Token header)"
echo "  sudo systemctl status betting-arb-monitor"
echo "  sudo journalctl -u betting-arb-monitor -f"
