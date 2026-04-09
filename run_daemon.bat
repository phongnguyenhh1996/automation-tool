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
coinmap-automation tv-watchlist-daemon >> "logs\daemon.log" 2>&1
set "EXIT_CODE=%ERRORLEVEL%"
echo [%date% %time%] INFO: Finished with exit code %EXIT_CODE%>> "logs\daemon.log"

exit /b %EXIT_CODE%

