#!/data/data/com.termux/files/usr/bin/bash
# StratBot one-shot setup inside Termux on Android.
# Run from the project folder:  bash deploy/termux_setup.sh
# Re-runnable: every step is idempotent.

set -euo pipefail
cd "$(dirname "$0")/.."

echo "[1/6] updating packages..."
pkg update -y >/dev/null

echo "[2/6] installing python and native wheels from the Termux repo..."
# numpy and pandas ship prebuilt for Termux's ARM python; do not let pip
# rebuild them from source, it would take an hour and often fails.
pkg install -y python python-numpy python-pandas >/dev/null

echo "[3/6] installing the pure-python deps via pip..."
pip install --upgrade pip -q
pip install -r requirements-termux.txt -q

echo "[4/6] sanity import check..."
python - <<'EOF'
import pandas, fastapi, uvicorn, yfinance
print("ok:", pandas.__version__, fastapi.__version__)
EOF

echo "[5/6] carrying over the paper ledger..."
ls data/paper_run.sqlite3 >/dev/null || echo "warning: data/paper_run.sqlite3 missing"

echo "[6/6] boot job (auto-start at phone restart, needs Termux:Boot app)..."
mkdir -p ~/.termux/boot
sed "s|__BOT_DIR__|$PWD|g" deploy/termux_boot.sh > ~/.termux/boot/stratbot.sh
chmod +x ~/.termux/boot/stratbot.sh

echo
echo "=== setup done ==="
echo "start it now with:  bash deploy/termux_start.sh"
echo "dashboard:          http://localhost:8000 (phone browser)"
echo "logs:               tail -f logs/termux.log"
