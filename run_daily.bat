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
cd /d "%~dp0"

REM Create logs directory if missing
if not exist "logs" mkdir "logs"

REM Activate virtual environment
call ".venv\Scripts\activate.bat"
if errorlevel 1 (
  echo [%date% %time%] ERROR: Failed to activate virtual environment.>> "logs\daily.log"
  exit /b 1
)

REM Run full pipeline (headless by default to save memory; add --headed manually if needed)
echo [%date% %time%] INFO: Starting coinmap-automation all>> "logs\daily.log"
coinmap-automation all --main-symbol XAUUSD >> "logs\daily.log" 2>&1
set "EXIT_CODE=%ERRORLEVEL%"
echo [%date% %time%] INFO: Finished with exit code %EXIT_CODE%>> "logs\daily.log"

REM Recycle daemon: tắt run_daemon đang chạy, chờ hết, bật lại cửa sổ mới (mã thoát = EXIT_CODE của all)
call "%~dp0stop_daemon_wait.bat"
start "automation-tool - run_daemon.bat" cmd /k "%~dp0run_daemon.bat"

exit /b %EXIT_CODE%
