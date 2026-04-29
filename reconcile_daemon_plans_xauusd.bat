@echo off
setlocal

title automation-tool - reconcile_daemon_plans_xauusd.bat
cd /d "%~dp0"

REM Giống reconcile-daemon-plans sau CLI: quét zones XAUUSD, spawn daemon-plan cho shard chưa terminal / chưa có PID.
set "AUTOMATION_MAIN_SYMBOL=XAUUSD"
set "ZONES=data\XAUUSD\zones"

call ".venv\Scripts\activate.bat"
if errorlevel 1 (
  echo [%date% %time%] ERROR: Không kích hoạt được .venv
  exit /b 1
)

echo [%date% %time%] INFO: reconcile-daemon-plans XAUUSD zones=%ZONES%
coinmap-automation reconcile-daemon-plans --zones-json "%ZONES%"
set "EXIT_CODE=%ERRORLEVEL%"
echo [%date% %time%] INFO: exit=%EXIT_CODE%
echo reconcile-daemon-plans XAUUSD | exit=%EXIT_CODE%

exit /b %EXIT_CODE%
