@echo off
setlocal

title automation-tool - reconcile_daemon_plans_xauusd.bat
cd /d "%~dp0"

REM Giống reconcile-daemon-plans sau CLI: quét zones XAUUSD, spawn daemon-plan cho shard chưa terminal / chưa có PID.
set "AUTOMATION_MAIN_SYMBOL=XAUUSD"
set "ZONES=data\XAUUSD\zones"

if not exist "logs" mkdir "logs"
set "LOG=logs\daemon_plan_xauusd.log"

call ".venv\Scripts\activate.bat"
if errorlevel 1 (
  echo [%date% %time%] ERROR: Không kích hoạt được .venv>> "%LOG%"
  exit /b 1
)

echo [%date% %time%] INFO: reconcile-daemon-plans XAUUSD zones=%ZONES%>> "%LOG%"
coinmap-automation reconcile-daemon-plans --zones-json "%ZONES%" >> "%LOG%" 2>&1
set "EXIT_CODE=%ERRORLEVEL%"
echo [%date% %time%] INFO: exit=%EXIT_CODE%>> "%LOG%"
echo reconcile-daemon-plans XAUUSD | exit=%EXIT_CODE% | log=%LOG%

exit /b %EXIT_CODE%
