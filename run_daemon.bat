@echo off
setlocal

title automation-tool - %~nx0
echo.
echo ============================================================
echo Running: %~nx0
echo CWD    : %cd%
echo Args   : %*
echo ============================================================

REM [Legacy] tv-watchlist-daemon: TradingView title -> shared memory / last.txt (daemon-plan lay gia tu MT5 ask/bid, khong doc IPC).
REM Sau Last hop le dau tien: tu dong reconcile-daemon-plans (spawn daemon-plan neu chua chay).
REM --stop-daemon-plans-on-exit: khi tat CMD hoac Ctrl+C se dung cac daemon-plan.
REM De ghi them last.txt: them --mirror-last-price-file [--last-price-file data\XAUUSD\last.txt]
REM Intended for Windows Task Scheduler ("At startup" / "On log on")
REM Browser: chạy browser_up.bat trước (hoặc lịch Task Scheduler riêng).

REM Ensure script runs from project root
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

REM Bắt buộc cặp active = XAUUSD (data/.main_chart_symbol bị ghi đè bởi env — xem images.get_active_main_symbol)
set "AUTOMATION_MAIN_SYMBOL=XAUUSD"

REM Activate virtual environment
call ".venv\Scripts\activate.bat" >> "%LOG_FILE%" 2>&1
if errorlevel 1 (
  echo [%date% %time%] ERROR: Failed to activate virtual environment.
  >> "%LOG_FILE%" echo [%date% %time%] ERROR: Failed to activate virtual environment.
  exit /b 1
)

echo [%date% %time%] INFO: Starting coinmap-automation tv-watchlist-daemon (gia) symbol=%AUTOMATION_MAIN_SYMBOL%
>> "%LOG_FILE%" echo [%date% %time%] INFO: Starting coinmap-automation tv-watchlist-daemon (gia) symbol=%AUTOMATION_MAIN_SYMBOL%
REM Headless by default; do NOT pass --headed.
coinmap-automation tv-watchlist-daemon --stop-daemon-plans-on-exit >> "%LOG_FILE%" 2>&1
set "EXIT_CODE=%ERRORLEVEL%"
echo [%date% %time%] INFO: Finished with exit code %EXIT_CODE%
>> "%LOG_FILE%" echo [%date% %time%] INFO: Finished with exit code %EXIT_CODE%

exit /b %EXIT_CODE%

