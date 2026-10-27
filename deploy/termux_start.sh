#!/data/data/com.termux/files/usr/bin/bash
# StratBot manual launcher for Termux (one cycle per SLEEP seconds, forever).
cd "$(dirname "$0")/.." || exit 1
mkdir -p logs
# The wake-lock keeps Android from freezing the process while the screen is off.
termux-wake-lock 2>/dev/null || true
export STRATBOT_CONTROL_TOKEN="${STRATBOT_CONTROL_TOKEN:-termux-$(openssl rand -hex 12)}"
echo "control token: $STRATBOT_CONTROL_TOKEN  (dashboard halt/resume)"
exec python run_bot.py --serve --trade --poll 900 \
     --db data/paper_run.sqlite3 --port 8000 \
     --symbols RELIANCE,TCS,INFY,HDFCBANK,ITC,SBIN,BTC,ETH,ICICIBANK,HINDUNILVR,BHARTIARTL,BAJFINANCE,KOTAKBANK,LT,AXISBANK,HCLTECH,ASIANPAINT,MARUTI,SUNPHARMA,TITAN,ULTRACEMCO,NTPC,POWERGRID,TATASTEEL,ADANIENT,ADANIPORTS,JSWSTEEL,TECHM,INDUSINDBK,HINDALCO,BAJAJFINSV,SBILIFE,DRREDDY,CIPLA,EICHERMOT,APOLLOHOSP,GRASIM,BRITANNIA,DIVISLAB,HEROMOTOCO,TATACONSUM \
     >> logs/termux.log 2>&1
