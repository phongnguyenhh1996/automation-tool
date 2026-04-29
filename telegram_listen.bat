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
REM Browser: chạy browser_up.bat trước (telegram-listen cần browser service).
cd /d "%~dp0"

REM Activate virtual environment
call ".venv\Scripts\activate.bat"
if errorlevel 1 (
  echo [%date% %time%] ERROR: Failed to activate virtual environment.
  exit /b 1
)

REM Listen inbound Telegram commands (/full, /update, /stop)
echo [%date% %time%] INFO: Starting coinmap-automation telegram-listen
coinmap-automation telegram-listen
set "EXIT_CODE=%ERRORLEVEL%"
echo [%date% %time%] INFO: Finished with exit code %EXIT_CODE%

exit /b %EXIT_CODE%
