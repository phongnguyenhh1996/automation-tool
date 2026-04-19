@echo off
setlocal

title automation-tool - stop_daemon_plan_xauusd.bat
cd /d "%~dp0"

REM Dừng mọi daemon-plan (file .daemon-plan-*.pid) trong zones XAUUSD — SIGTERM.
set "AUTOMATION_MAIN_SYMBOL=XAUUSD"
set "ZONES=data\XAUUSD\zones"

if not exist "logs" mkdir "logs"
set "LOG=logs\daemon_plan_xauusd.log"

call ".venv\Scripts\activate.bat"
if errorlevel 1 (
  echo [%date% %time%] ERROR: Không kích hoạt được .venv>> "%LOG%"
  exit /b 1
)

echo [%date% %time%] INFO: stop-daemon-plans XAUUSD zones=%ZONES%>> "%LOG%"
coinmap-automation stop-daemon-plans --zones-json "%ZONES%" >> "%LOG%" 2>&1
set "EXIT_CODE=%ERRORLEVEL%"
echo [%date% %time%] INFO: exit=%EXIT_CODE%>> "%LOG%"
echo stop-daemon-plans XAUUSD | exit=%EXIT_CODE% | log=%LOG%

exit /b %EXIT_CODE%
