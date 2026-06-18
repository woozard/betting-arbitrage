#!/bin/bash
# One-time (or periodic) manual login into a persistent Chrome profile for Sports411.
# After you log in and place a test bet manually, the bot can reuse this profile via
# SPORTS411_CHROME_USER_DATA_DIR (set in .env or systemd).
#
# Usage on EC2:
#   bash seed_s411_chrome_profile.sh
#   # connect VNC (see start_ec2_vnc_browser.sh), log in to Sports411, place a test bet
#
# Then run placement test:
#   export SPORTS411_CHROME_USER_DATA_DIR="$HOME/.sports411-chrome-profile"
#   xvfb-run -a venv/bin/python3 test_s411_place_bet.py --attach --no-proxy --list-only

set -e

PROFILE_DIR="${SPORTS411_CHROME_USER_DATA_DIR:-$HOME/.sports411-chrome-profile}"
DEBUG_PORT="${SPORTS411_CHROME_DEBUG_PORT:-9222}"
export DISPLAY="${DISPLAY:-:99}"
mkdir -p "$PROFILE_DIR"

echo "Profile: $PROFILE_DIR"
echo "Debug port: $DEBUG_PORT"
echo "Launching Chrome (log in manually via VNC if needed)..."

google-chrome-stable \
  --remote-debugging-port="$DEBUG_PORT" \
  --user-data-dir="$PROFILE_DIR" \
  --no-sandbox \
  --disable-dev-shm-usage \
  --window-size=1920,1080 \
  "https://be.sports411.ag/en/sports/baseball/mlb/game-lines/"
