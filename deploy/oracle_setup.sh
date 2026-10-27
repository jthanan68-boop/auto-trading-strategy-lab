#!/usr/bin/env bash
# One-shot setup for StratBot on the Oracle Cloud Always Free VM (Ubuntu 22.04/24.04).
# Run once, as the login user:  bash deploy/oracle_setup.sh
# Re-runnable: every step is idempotent.

set -euo pipefail
APP=/opt/startbot

sudo apt-get update -y
sudo apt-get install -y python3-venv python3-pip

# Virtual env + dependency install (k/time and pandas ship aarch64 wheels).
if [ ! -d "$APP/.venv" ]; then
  python3 -m venv "$APP/.venv"
fi
"$APP/.venv/bin/pip" install --upgrade pip
"$APP/.venv/bin/pip" install -r "$APP/requirements.txt"

# systemd service: auto-start at boot, auto-restart on crash.
sudo cp "$APP/deploy/startbot.service" /etc/systemd/system/startbot.service
sudo sed -i "s/change-this-token/$(tr -dc A-Za-z0-9 </dev/urandom | head -c 24)/" /etc/systemd/system/startbot.service
sudo systemctl daemon-reload
sudo systemctl enable --now startbot.service

echo "waiting for the engine to come up..."
sleep 8
curl -s http://127.0.0.1:8000/api/status | head -c 400
echo
echo "=== setup done ==="
echo "API     : reachable only on this VM (port 8000 stays internal)"
echo "logs    : journalctl -u startbot -f"
echo "restart : sudo systemctl restart startbot"
