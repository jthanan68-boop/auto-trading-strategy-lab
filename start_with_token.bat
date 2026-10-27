@echo off
cd /d "%~dp0"

set "VENV=.venv\Scripts\python.exe"
set "DB=data\paper_run.sqlite3"
set "SLEEP=60"
set "PORT=8000"
set "DASHBOARD=dashboard\dist\index.html"
set "SYMBOLS=RELIANCE,TCS,INFY,HDFCBANK,ITC,SBIN,BHARTIARTL,KOTAKBANK,LT,AXISBANK"

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

rem Set the control token to match the dashboard's VITE_CONTROL_TOKEN
if "%STRATBOT_CONTROL_TOKEN%"=="" (
  set "STRATBOT_CONTROL_TOKEN=local-3456721987351200"
)

echo.
echo   StratBot  -  paper trading engine
echo   ---------------------------------------------
echo   project: %CD%
echo.

echo   dashboard : http://127.0.0.1:%PORT%/
echo   database  : %DB%
echo   universe  : %SYMBOLS%
echo   trading   : ON, one cycle every %SLEEP%s in this same process
echo   mode      : PAPER  (this process cannot place a real order)
echo.
echo   The engine decides on completed bars only and fills at the next bar's
echo   open; NSE quotes arrive about 15 minutes late, so a quiet book during
echo   a refreshed cache is expected, not a fault.
echo.
echo   Press Ctrl+C to stop.
echo   ---------------------------------------------
echo.

rem Give uvicorn a moment to bind before the browser asks for the page.
start "" /b cmd /c "timeout /t 5 /nobreak >nul && start "" http://127.0.0.1:%PORT%/"

rem --refresh: a cold start on empty caches fails closed without it.
%VENV% run_bot.py --serve --trade --refresh --poll %SLEEP% --db "%DB%" --symbols "%SYMBOLS%" --capital 5000 %*