@echo off
setlocal

REM 24/24 daemon: TradingView watchlist + zones_state orchestration
REM Intended for Windows Task Scheduler ("At startup" / "On log on")

REM Ensure script runs from project root
cd /d "%~dp0"

REM Create logs directory if missing
if not exist "logs" mkdir "logs"

REM Activate virtual environment
call ".venv\Scripts\activate.bat"
if errorlevel 1 (
  echo [%date% %time%] ERROR: Failed to activate virtual environment.>> "logs\daemon.log"
  exit /b 1
)

echo [%date% %time%] INFO: Ensuring browser service is up>> "logs\daemon.log"
coinmap-automation browser up >> "logs\daemon.log" 2>&1
if errorlevel 1 (
  echo [%date% %time%] ERROR: browser service failed to start.>> "logs\daemon.log"
  exit /b %ERRORLEVEL%
)

echo [%date% %time%] INFO: Starting coinmap-automation tv-watchlist-daemon>> "logs\daemon.log"
REM Headless by default; do NOT pass --headed.
set "ZONES_JSON=data\XAUUSD\zones_state.json"
if not exist "%ZONES_JSON%" (
  echo [%date% %time%] ERROR: zones_state.json not found at %ZONES_JSON%>> "logs\daemon.log"
  echo [%date% %time%] ERROR: Run coinmap-automation all/update to generate zones_state.json.>> "logs\daemon.log"
  exit /b 2
)
coinmap-automation tv-watchlist-daemon --zones-json "%ZONES_JSON%" >> "logs\daemon.log" 2>&1
set "EXIT_CODE=%ERRORLEVEL%"
echo [%date% %time%] INFO: Finished with exit code %EXIT_CODE%>> "logs\daemon.log"

exit /b %EXIT_CODE%

