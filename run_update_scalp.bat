@echo off
setlocal

title automation-tool - %~nx0
echo.
echo ============================================================
echo Running: %~nx0
echo CWD    : %cd%
echo Args   : %*
echo ============================================================

REM Scalp intraday update: TradingView 15m ICT + 5m then Coinmap M5 (same as run_update.bat), OpenAI asks for best scalp plan.
REM - Thread OpenAI rieng (last_scalp_response_id.txt)
REM - Zone labels: scalp_<id> (vi du: scalp_1, scalp_2, scalp_3)
REM - Zones luu vao data\XAUUSD\zones\ cung voi zones thuong
REM - Sau khi ghi zones: spawn daemon-plan cho cac shard scalp_* moi
cd /d "%~dp0"

call ".venv\Scripts\activate.bat"
if errorlevel 1 (
  echo [%date% %time%] ERROR: Failed to activate virtual environment.
  exit /b 1
)

echo [%date% %time%] INFO: Starting coinmap-automation update-scalp
REM update-scalp: ghi accounts-scalp.json cạnh accounts.json, set MT5_ACCOUNTS_JSON, reconcile daemon-plan.
REM Thêm --mt5-accounts-json đường dẫn nếu không dùng biến môi trường MT5_ACCOUNTS_JSON.
coinmap-automation update-scalp %*
set "EXIT_CODE=%ERRORLEVEL%"
echo [%date% %time%] INFO: update-scalp finished with exit code %EXIT_CODE%

exit /b %EXIT_CODE%
