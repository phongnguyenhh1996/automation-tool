@echo off
setlocal

REM Ensure script runs from project root
cd /d "%~dp0"

REM Create logs directory if missing
if not exist "logs" mkdir "logs"

REM Activate virtual environment
call ".venv\Scripts\activate.bat"
if errorlevel 1 (
  echo [%date% %time%] ERROR: Failed to activate virtual environment.>> "logs\telegram_listen.log"
  exit /b 1
)

REM Listen inbound Telegram commands (/full, /update, /stop)
echo [%date% %time%] INFO: Starting coinmap-automation telegram-listen>> "logs\telegram_listen.log"
coinmap-automation telegram-listen >> "logs\telegram_listen.log" 2>&1
set "EXIT_CODE=%ERRORLEVEL%"
echo [%date% %time%] INFO: Finished with exit code %EXIT_CODE%>> "logs\telegram_listen.log"

exit /b %EXIT_CODE%
