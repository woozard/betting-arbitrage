#!/bin/bash
# Stop EC2 VNC browser test session started by start_ec2_vnc_browser.sh

DISPLAY_NUM=99

echo "Stopping VNC test session..."
pkill -f "google-chrome.*sports411" 2>/dev/null || true
pkill -f "google-chrome-stable" 2>/dev/null || true
pkill -f "x11vnc.*:${DISPLAY_NUM}" 2>/dev/null || true
pkill -f "fluxbox" 2>/dev/null || true
pkill -f "Xvfb :${DISPLAY_NUM}" 2>/dev/null || true
sleep 1
echo "Done."
