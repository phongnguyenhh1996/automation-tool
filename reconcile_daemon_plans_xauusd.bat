@echo off
setlocal

title automation-tool - reconcile_daemon_plans_xauusd.bat
cd /d "%~dp0"
set "LOG_DIR=%~dp0logs"
if not exist "%LOG_DIR%" mkdir "%LOG_DIR%"
set "LOG_FILE=%LOG_DIR%\%~n0.log"
echo [%date% %time%] INFO: Logging to "%LOG_FILE%"
>> "%LOG_FILE%" echo.
>> "%LOG_FILE%" echo ============================================================
>> "%LOG_FILE%" echo [%date% %time%] Running: %~nx0
>> "%LOG_FILE%" echo CWD    : %cd%
>> "%LOG_FILE%" echo Args   : %*
>> "%LOG_FILE%" echo ============================================================

REM Giống reconcile-daemon-plans sau CLI: quét zones XAUUSD, spawn daemon-plan cho shard chưa terminal / chưa có PID.
set "AUTOMATION_MAIN_SYMBOL=XAUUSD"
set "ZONES=data\XAUUSD\zones"

call ".venv\Scripts\activate.bat" >> "%LOG_FILE%" 2>&1
if errorlevel 1 (
  echo [%date% %time%] ERROR: Không kích hoạt được .venv
  >> "%LOG_FILE%" echo [%date% %time%] ERROR: Không kích hoạt được .venv
  exit /b 1
)

echo [%date% %time%] INFO: reconcile-daemon-plans XAUUSD zones=%ZONES%
>> "%LOG_FILE%" echo [%date% %time%] INFO: reconcile-daemon-plans XAUUSD zones=%ZONES%
coinmap-automation reconcile-daemon-plans --zones-json "%ZONES%" >> "%LOG_FILE%" 2>&1
set "EXIT_CODE=%ERRORLEVEL%"
echo [%date% %time%] INFO: exit=%EXIT_CODE%
>> "%LOG_FILE%" echo [%date% %time%] INFO: exit=%EXIT_CODE%
echo reconcile-daemon-plans XAUUSD | exit=%EXIT_CODE%

exit /b %EXIT_CODE%
