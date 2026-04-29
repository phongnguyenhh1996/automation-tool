@echo off
setlocal

title automation-tool - %~nx0
echo.
echo ============================================================
echo Running: %~nx0
echo CWD    : %cd%
echo Args   : %*
echo ============================================================

REM USDJPY: capture charts -> analyze OpenAI; MT5 chỉ dry-run (không lệnh thật)
REM Mặc định headless (tiết kiệm RAM). Nếu cần debug UI: thêm --headed vào lệnh capture bên dưới.
REM Browser: chạy browser_up.bat trước.
cd /d "%~dp0"

call ".venv\Scripts\activate.bat"
if errorlevel 1 (
  echo ERROR: Failed to activate virtual environment.
  exit /b 1
)

echo [%date% %time%] capture --main-symbol USDJPY
coinmap-automation capture --main-symbol USDJPY
if errorlevel 1 (
  echo ERROR: capture failed code %ERRORLEVEL%.
  exit /b %ERRORLEVEL%
)

echo [%date% %time%] analyze --main-symbol USDJPY --mt5-dry-run --telegram-detail-chat-id -1003344625474
coinmap-automation analyze --main-symbol USDJPY --mt5-dry-run --telegram-detail-chat-id -1003344625474
set "EXIT_CODE=%ERRORLEVEL%"
echo [%date% %time%] Finished exit code %EXIT_CODE%

if %EXIT_CODE% neq 0 (
  echo ERROR: analyze failed code %EXIT_CODE%.
) else (
  echo OK.
)
exit /b %EXIT_CODE%
