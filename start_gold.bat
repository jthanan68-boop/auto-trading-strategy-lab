@echo off
setlocal enabledelayedexpansion

rem Launch StratBot gold-only section.
rem Gold is a commodity future (GC=F) with its own session; runs independently of start.bat.
rem Separate DB and port so it can run alongside equities/crypto without clashing.

cd /d "%~dp0"

set "VENV=.venv\Scripts\python.exe"
set "DB=data\gold.sqlite3"
set "SLEEP=300"
set "PORT=8002"
set "DASHBOARD=dashboard\dist\index.html"
set "SYMBOLS=GOLD"

echo.
echo   StratBot  -  gold only
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
echo   universe  : %SYMBOLS%  (gold - commodity, separate session)
echo   trading   : ON, one cycle every %SLEEP%s in this same process
echo   mode      : PAPER  (this process cannot place a real order)
echo.
echo   Press Ctrl+C to stop.
echo   ---------------------------------------------
echo.

rem Give uvicorn a moment to bind before the browser asks for the page.
start "" /b cmd /c "timeout /t 5 /nobreak >nul && start "" http://127.0.0.1:%PORT%/"

"%VENV%" run_bot.py --serve --trade --refresh --poll %SLEEP% --db "%DB%" --symbols "%SYMBOLS%" --style intraday --interval 5m --port %PORT% --portfolio --portfolio-cadence weekly %*

endlocal
