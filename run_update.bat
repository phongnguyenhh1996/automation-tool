@echo off
setlocal

title automation-tool - %~nx0
echo.
echo ============================================================
echo Running: %~nx0
echo CWD    : %cd%
echo Args   : %*
echo ============================================================

REM Intraday update: first run after all = morning_full_analysis.json + M15/M5 (new thread); later runs = M15/M5 only + chain; then TV sync if zones changed
cd /d "%~dp0"

if not exist "logs" mkdir "logs"

call ".venv\Scripts\activate.bat"
if errorlevel 1 (
  echo [%date% %time%] ERROR: Failed to activate virtual environment.>> "logs\update.log"
  exit /b 1
)

echo [%date% %time%] INFO: Starting coinmap-automation update>> "logs\update.log"
coinmap-automation update --main-symbol XAUUSD >> "logs\update.log" 2>&1
set "EXIT_CODE=%ERRORLEVEL%"
echo [%date% %time%] INFO: Finished with exit code %EXIT_CODE%>> "logs\update.log"

exit /b %EXIT_CODE%
