@echo off
setlocal enabledelayedexpansion

rem Launch StratBot from wherever this file lives, so the app always starts in
rem the project directory regardless of the shell's current location.

cd /d "%~dp0"

set "VENV=.venv\Scripts\python.exe"
set "DB=data\paper_run.sqlite3"
set "SLEEP=60"
set "PORT=8000"
set "DASHBOARD=dashboard\dist\index.html"
set "SYMBOLS=RELIANCE,TCS,INFY,HDFCBANK,ITC,SBIN,BTC,ETH,SOL,XRP,ADA,DOGE,AVAX,LINK,DOT,LTC,BCH,TRX,ICICIBANK,HINDUNILVR,BHARTIARTL,BAJFINANCE,KOTAKBANK,LT,AXISBANK,HCLTECH,ASIANPAINT,MARUTI,SUNPHARMA,TITAN,ULTRACEMCO,NTPC,POWERGRID,TATASTEEL,ADANIENT,ADANIPORTS,JSWSTEEL,TECHM,INDUSINDBK,HINDALCO,BAJAJFINSV,SBILIFE,DRREDDY,CIPLA,EICHERMOT,APOLLOHOSP,GRASIM,BRITANNIA,DIVISLAB,HEROMOTOCO,TATACONSUM,CL,BRENT,NATGAS,GOLD,SILVER,COPPER,PLATINUM,PALLADIUM,EURUSD,GBPUSD,USDJPY,AUDUSD,USDCAD,USDCHF,NZDUSD,EURGBP,EURJPY,GBPJPY,EURTRY,USDTRY,USDMXN,USDZAR,USDSGD,USDHKD"

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
if "%STRATBOT_CONTROL_TOKEN%"=="" set "STRATBOT_CONTROL_TOKEN=local-3456721987351200"

echo   dashboard : http://127.0.0.1:%PORT%/
echo   database  : %DB%
echo   universe  : %SYMBOLS%
echo   trading   : ON, one cycle every %SLEEP%s in this same process
echo   mode      : PAPER  (this process cannot place a real order)
echo   portfolio : enabled (momentum-ranked, vol-sized, sector-capped)
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

rem Supervisor. An unattended run used to stay dead after any crash until
rem someone noticed; this relaunches it. Exit code 2 is the instance lock
rem refusing a second copy, so restarting that only spins. Ctrl+C or closing
rem the window ends the supervisor as well as the engine.
set "RESTARTS=0"
:run
rem --refresh: a cold start on empty caches fails closed without it.
"%VENV%" run_bot.py --serve --trade --refresh --poll %SLEEP% --db "%DB%" --symbols "%SYMBOLS%" --portfolio %*
set "RC=%ERRORLEVEL%"
if "%RC%"=="0" goto stopped
if "%RC%"=="2" goto stopped
set /a RESTARTS+=1
if %RESTARTS% GEQ 20 goto gaveup
echo.
echo   [X] engine exited with code %RC% - restarting in 10s (%RESTARTS% of 20)
timeout /t 10 /nobreak >nul
goto run

:gaveup
echo.
echo   [X] engine exited 20 times; supervisor stopped rather than loop for ever.

:stopped
endlocal
