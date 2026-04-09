@echo off
setlocal

REM Ensure script runs from project root
cd /d "%~dp0"

REM Create logs directory if missing
if not exist "logs" mkdir "logs"

REM Activate virtual environment
call ".venv\Scripts\activate.bat"
if errorlevel 1 (
  echo [%date% %time%] ERROR: Failed to activate virtual environment.>> "logs\capture_headed.log"
  exit /b 1
)

echo [%date% %time%] INFO: Ensuring browser service is up>> "logs\capture_headed.log"
coinmap-automation browser up >> "logs\capture_headed.log" 2>&1
if errorlevel 1 (
  echo [%date% %time%] ERROR: browser service failed to start.>> "logs\capture_headed.log"
  exit /b %ERRORLEVEL%
)

REM Capture charts (headless by default; this script name is legacy)
echo [%date% %time%] INFO: Starting coinmap-automation capture>> "logs\capture_headed.log"
coinmap-automation capture --main-symbol XAUUSD >> "logs\capture_headed.log" 2>&1
set "EXIT_CODE=%ERRORLEVEL%"
echo [%date% %time%] INFO: Finished with exit code %EXIT_CODE%>> "logs\capture_headed.log"

exit /b %EXIT_CODE%
