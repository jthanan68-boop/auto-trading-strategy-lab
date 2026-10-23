@echo off
setlocal enabledelayedexpansion

rem Launch StratBot from wherever this file lives, so the app always starts in
rem the project directory regardless of the shell's current location.

cd /d "%~dp0"

set "VENV=.venv\Scripts\python.exe"
set "DB=data\paper_run.sqlite3"
set "SLEEP=900"
set "PORT=8000"
set "DASHBOARD=dashboard\dist\index.html"
set "SYMBOLS=RELIANCE,TCS,INFY,HDFCBANK,ITC,SBIN,BTC,ETH,ICICIBANK,HINDUNILVR,BHARTIARTL,BAJFINANCE,KOTAKBANK,LT,AXISBANK,HCLTECH,ASIANPAINT,MARUTI,SUNPHARMA,TITAN,ULTRACEMCO,NTPC,POWERGRID,TATASTEEL,ADANIENT,ADANIPORTS,JSWSTEEL,TECHM,INDUSINDBK,HINDALCO,BAJAJFINSV,SBILIFE,DRREDDY,CIPLA,EICHERMOT,APOLLOHOSP,GRASIM,BRITANNIA,DIVISLAB,HEROMOTOCO,TATACONSUM"

echo.
echo   StratBot  -  paper trading engine
echo   ---------------------------------------------
echo   project: %CD%
echo.

if not exist "%VENV%" (
  echo   [X] Virtual environment not found: %VENV%
  echo.
  echo       Create it with:
  echo           python -m venv .venv
  echo           .venv\Scripts\pip install -r requirements.txt
  echo.
  pause
  exit /b 1
)

if not exist "%DASHBOARD%" (
  echo   [X] Dashboard has not been built.
  echo.
  echo       Build it with:
  echo           cd dashboard
  echo           npm install
  echo           npm run build
  echo.
  pause
  exit /b 1
)

rem Without a token the emergency-stop and resume endpoints fail closed, so the
rem dashboard would show every control disabled.
if "%STRATBOT_CONTROL_TOKEN%"=="" set "STRATBOT_CONTROL_TOKEN=local-%RANDOM%%RANDOM%%RANDOM%"

echo   dashboard : http://127.0.0.1:%PORT%/
echo   database  : %DB%
echo   universe  : %SYMBOLS%
echo   trading   : ON, one cycle every %SLEEP%s in this same process
echo   mode      : PAPER  (this process cannot place a real order)
echo.
echo   The engine decides on one daily candle and fills on the next, so a
echo   quiet dashboard between sessions is expected, not a fault.
echo.
echo   Press Ctrl+C to stop.
echo   ---------------------------------------------
echo.

rem Give uvicorn a moment to bind before the browser asks for the page.
start "" /b cmd /c "timeout /t 5 /nobreak >nul && start "" http://127.0.0.1:%PORT%/"

"%VENV%" run_bot.py --serve --trade --poll %SLEEP% --db "%DB%" --symbols "%SYMBOLS%" %*

endlocal
