@echo off
setlocal enabledelayedexpansion

rem Launch StratBot crypto section - 24/7, no session square-off.
rem Runs independently of start.bat (equities). Separate DB and port.

cd /d "%~dp0"

set "VENV=.venv\Scripts\python.exe"
set "DB=data\crypto.sqlite3"
set "SLEEP=300"
set "PORT=8001"
set "DASHBOARD=dashboard\dist\index.html"
set "SYMBOLS=BTC,ETH"

echo.
echo   StratBot  -  crypto section (24/7)
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

if "%STRATBOT_CONTROL_TOKEN%"=="" set "STRATBOT_CONTROL_TOKEN=local-3456721987351200"

echo   dashboard : http://127.0.0.1:%PORT%/
echo   database  : %DB%
echo   universe  : %SYMBOLS%  (crypto - trades 24/7, no square-off)
echo   trading   : ON, one cycle every %SLEEP%s in this same process
echo   mode      : PAPER  (this process cannot place a real order)
echo.
echo   Crypto positions are never squared off at a session close.
echo   Press Ctrl+C to stop.
echo   ---------------------------------------------
echo.

rem Give uvicorn a moment to bind before the browser asks for the page.
start "" /b cmd /c "timeout /t 5 /nobreak >nul && start "" http://127.0.0.1:%PORT%/"

"%VENV%" run_bot.py --serve --trade --refresh --poll %SLEEP% --db "%DB%" --symbols "%SYMBOLS%" --style intraday --interval 5m --port %PORT% --portfolio --portfolio-cadence weekly %*

endlocal
