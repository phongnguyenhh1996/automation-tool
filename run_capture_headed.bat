@echo off
setlocal

title automation-tool - %~nx0
echo.
echo ============================================================
echo Running: %~nx0
echo CWD    : %cd%
echo Args   : %*
echo ============================================================

REM Ensure script runs from project root
REM Browser: chạy browser_up.bat trước khi capture.
cd /d "%~dp0"

REM Activate virtual environment
call ".venv\Scripts\activate.bat"
if errorlevel 1 (
  echo [%date% %time%] ERROR: Failed to activate virtual environment.
  exit /b 1
)

REM Capture charts (headless by default; this script name is legacy)
echo [%date% %time%] INFO: Starting coinmap-automation capture
coinmap-automation capture --main-symbol XAUUSD
set "EXIT_CODE=%ERRORLEVEL%"
echo [%date% %time%] INFO: Finished with exit code %EXIT_CODE%

exit /b %EXIT_CODE%
