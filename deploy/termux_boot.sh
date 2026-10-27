#!/data/data/com.termux/files/usr/bin/bash
# StratBot boot job, installed by termux_setup.sh into ~/.termux/boot/.
# Runs when the phone restarts (requires the Termux:Boot companion app).
bot_dir="__BOT_DIR__"
sleep 10   # let network/wifi settle before the first refresh
cd "$bot_dir" || exit 1
termux-wake-lock 2>/dev/null || true
export STRATBOT_CONTROL_TOKEN="${STRATBOT_CONTROL_TOKEN:-termux-$(openssl rand -hex 12)}"
python run_bot.py --serve --trade --poll 900 \
     --db data/paper_run.sqlite3 --port 8000 \
     --symbols RELIANCE,TCS,INFY,HDFCBANK,ITC,SBIN,BTC,ETH,ICICIBANK,HINDUNILVR,BHARTIARTL,BAJFINANCE,KOTAKBANK,LT,AXISBANK,HCLTECH,ASIANPAINT,MARUTI,SUNPHARMA,TITAN,ULTRACEMCO,NTPC,POWERGRID,TATASTEEL,ADANIENT,ADANIPORTS,JSWSTEEL,TECHM,INDUSINDBK,HINDALCO,BAJAJFINSV,SBILIFE,DRREDDY,CIPLA,EICHERMOT,APOLLOHOSP,GRASIM,BRITANNIA,DIVISLAB,HEROMOTOCO,TATACONSUM \
     >> logs/termux.log 2>&1
