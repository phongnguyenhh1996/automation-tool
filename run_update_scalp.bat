@echo off
setlocal

title automation-tool - %~nx0
echo.
echo ============================================================
echo Running: %~nx0
echo CWD    : %cd%
echo Args   : %*
echo ============================================================

REM Scalp intraday update: TradingView 15m ICT + 5m, Coinmap M5 JSON + PNG, OpenAI scalp plan.
REM - Thread OpenAI rieng (last_scalp_response_id.txt)
REM - Zone labels: scalp_<id> (vi du: scalp_1, scalp_2, scalp_3)
REM - Zones luu vao data\XAUUSD\zones\ cung voi zones thuong
REM - Sau khi ghi zones: spawn daemon-plan cho cac shard scalp_* moi
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

call ".venv\Scripts\activate.bat" >> "%LOG_FILE%" 2>&1
if errorlevel 1 (
  echo [%date% %time%] ERROR: Failed to activate virtual environment.
  >> "%LOG_FILE%" echo [%date% %time%] ERROR: Failed to activate virtual environment.
  exit /b 1
)

REM Coinmap M5 footprint (headless by default; add --headed or --gocharting manually if needed)
echo [%date% %time%] INFO: Starting coinmap-automation update-scalp
>> "%LOG_FILE%" echo [%date% %time%] INFO: Starting coinmap-automation update-scalp
REM update-scalp: ghi accounts-scalp.json cạnh accounts.json, set MT5_ACCOUNTS_JSON, reconcile daemon-plan.
REM Thêm --mt5-accounts-json đường dẫn nếu không dùng biến môi trường MT5_ACCOUNTS_JSON.
coinmap-automation update-scalp %* >> "%LOG_FILE%" 2>&1
set "EXIT_CODE=%ERRORLEVEL%"
echo [%date% %time%] INFO: update-scalp finished with exit code %EXIT_CODE%
>> "%LOG_FILE%" echo [%date% %time%] INFO: update-scalp finished with exit code %EXIT_CODE%

exit /b %EXIT_CODE%
