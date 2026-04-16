@echo off
setlocal

title automation-tool - %~nx0
echo.
echo ============================================================
echo Running: %~nx0
echo CWD    : %cd%
echo Args   : %*
echo ============================================================

REM 24/24 daemon giá: TradingView watchlist Last -> data\<SYM>\last.txt (daemon-plan đọc file này)
REM Intended for Windows Task Scheduler ("At startup" / "On log on")
REM Browser: chạy browser_up.bat trước (hoặc lịch Task Scheduler riêng).

REM Ensure script runs from project root
cd /d "%~dp0"

REM Bắt buộc cặp active = XAUUSD (data/.main_chart_symbol bị ghi đè bởi env — xem images.get_active_main_symbol)
set "AUTOMATION_MAIN_SYMBOL=XAUUSD"

REM Create logs directory if missing
if not exist "logs" mkdir "logs"

REM Activate virtual environment
call ".venv\Scripts\activate.bat"
if errorlevel 1 (
  echo [%date% %time%] ERROR: Failed to activate virtual environment.>> "logs\daemon.log"
  exit /b 1
)

echo [%date% %time%] INFO: Starting coinmap-automation tv-watchlist-daemon (gia) symbol=%AUTOMATION_MAIN_SYMBOL%>> "logs\daemon.log"
REM Headless by default; do NOT pass --headed.
set "LAST_PRICE=data\XAUUSD\last.txt"
coinmap-automation tv-watchlist-daemon --last-price-file "%LAST_PRICE%" >> "logs\daemon.log" 2>&1
set "EXIT_CODE=%ERRORLEVEL%"
echo [%date% %time%] INFO: Finished with exit code %EXIT_CODE%>> "logs\daemon.log"

exit /b %EXIT_CODE%

