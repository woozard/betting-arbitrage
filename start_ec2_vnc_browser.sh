#!/bin/bash
# Start a temporary VNC desktop on EC2 so you can test manual Sports411 betting
# from the server's IP (same environment as the bot).
#
# On your Mac (new terminal):
#   ssh -i "/Users/carlosmoralescliment/Downloads/pinn-arb-new (1).pem" -L 5900:localhost:5900 ubuntu@100.53.194.189
#
# Then open Screen Sharing / VNC client to:  localhost:5900
# (Mac: open vnc://localhost:5900)

set -e

DISPLAY_NUM=99
export DISPLAY=:${DISPLAY_NUM}
VNC_PORT=5900
VNC_PASS="${VNC_PASS:-arbtest99}"
PASSWD_FILE="$HOME/.vnc/passwd_ec2_test"
LOG_DIR="$HOME/betting-arbitrage/logs"
mkdir -p "$LOG_DIR"

echo "=== EC2 VNC browser test session ==="

# Stop any previous test session
pkill -f "Xvfb :${DISPLAY_NUM}" 2>/dev/null || true
pkill -f "x11vnc.*:${DISPLAY_NUM}" 2>/dev/null || true
pkill -f "fluxbox" 2>/dev/null || true
sleep 1

echo "Starting virtual display :${DISPLAY_NUM}..."
Xvfb :${DISPLAY_NUM} -screen 0 1920x1080x24 >>"$LOG_DIR/vnc_xvfb.log" 2>&1 &
sleep 2

echo "Starting window manager..."
fluxbox >>"$LOG_DIR/vnc_fluxbox.log" 2>&1 &
sleep 1

echo "Starting VNC server on localhost:${VNC_PORT}..."
mkdir -p "$HOME/.vnc"
x11vnc -storepasswd "$VNC_PASS" "$PASSWD_FILE" >/dev/null
x11vnc -display :${DISPLAY_NUM} -rfbauth "$PASSWD_FILE" -listen 127.0.0.1 -rfbport ${VNC_PORT} \
  -xkb -forever -shared -bg -o "$LOG_DIR/vnc_server.log"

echo "Launching Chrome to Sports411 MLB lines..."
PROFILE_DIR="${SPORTS411_CHROME_USER_DATA_DIR:-$HOME/.sports411-chrome-profile}"
mkdir -p "$PROFILE_DIR"
google-chrome-stable \
  --remote-debugging-port="${SPORTS411_CHROME_DEBUG_PORT:-9222}" \
  --user-data-dir="$PROFILE_DIR" \
  --no-sandbox \
  --disable-dev-shm-usage \
  --window-size=1920,1080 \
  "https://be.sports411.ag/en/sports/baseball/mlb/game-lines/" \
  >>"$LOG_DIR/vnc_chrome.log" 2>&1 &

echo ""
echo "=== Ready ==="
echo "1. On your Mac, open a NEW terminal and run:"
echo '   ssh -i "/Users/carlosmoralescliment/Downloads/pinn-arb-new (1).pem" -L 5900:localhost:5900 ubuntu@100.53.194.189'
echo ""
echo "2. Connect VNC to:  vnc://localhost:5900"
echo "   Password:        ${VNC_PASS}"
echo "   Mac shortcut:    open vnc://localhost:5900"
echo ""
echo "3. Log in to Sports411 (account 8715) and try a manual \$25 bet."
echo ""
echo "   Optional: use persistent profile for bot attach tests:"
echo "   export SPORTS411_CHROME_USER_DATA_DIR=\"\$HOME/.sports411-chrome-profile\""
echo "   (Chrome above already uses this profile + debug port 9222)"
echo ""
echo "4. When done, stop with:  bash stop_ec2_vnc_browser.sh"
echo ""
echo "Logs: $LOG_DIR/vnc_*.log"
