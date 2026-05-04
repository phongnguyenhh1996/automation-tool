@echo off
setlocal

title automation-tool - %~nx0
echo.
echo ============================================================
echo Running: %~nx0
echo CWD    : %cd%
echo Args   : %*
echo ============================================================

REM Scalp intraday update: same flow as run_update.bat but asks for best scalp plan.
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
coinmap-automation update-scalp --main-symbol XAUUSD %*
set "EXIT_CODE=%ERRORLEVEL%"
echo [%date% %time%] INFO: update-scalp finished with exit code %EXIT_CODE%

if %EXIT_CODE% neq 0 (
  echo [%date% %time%] WARN: update-scalp failed, skipping reconcile-daemon-plans.
  exit /b %EXIT_CODE%
)

echo [%date% %time%] INFO: Spawning daemon-plan for new scalp zones...
coinmap-automation reconcile-daemon-plans --zones-json "data\XAUUSD\zones"
set "REC_EXIT=%ERRORLEVEL%"
echo [%date% %time%] INFO: reconcile-daemon-plans finished with exit code %REC_EXIT%

exit /b %EXIT_CODE%
