#!/bin/bash
# Sports411 betting uses plain Chrome (attach mode) + xdotool trusted clicks.
# Requires: xvfb, xdotool, google-chrome-stable
set -e
cd "$(dirname "$0")"
if ! command -v xdotool >/dev/null 2>&1; then
  echo "xdotool is required. Install with: sudo apt-get install -y xdotool"
  exit 1
fi
exec xvfb-run -a venv/bin/python sports411_betting.py
