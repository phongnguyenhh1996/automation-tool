@echo off
setlocal

REM Ensure script runs from project root
cd /d "%~dp0"

REM Create logs directory if missing
if not exist "logs" mkdir "logs"

REM Activate virtual environment
call ".venv\Scripts\activate.bat"
if errorlevel 1 (
  echo [%date% %time%] ERROR: Failed to activate virtual environment.>> "logs\daily.log"
  exit /b 1
)

REM Run full pipeline and append logs
echo [%date% %time%] INFO: Starting coinmap-automation all>> "logs\daily.log"
coinmap-automation all >> "logs\daily.log" 2>&1
set "EXIT_CODE=%ERRORLEVEL%"
echo [%date% %time%] INFO: Finished with exit code %EXIT_CODE%>> "logs\daily.log"

exit /b %EXIT_CODE%
