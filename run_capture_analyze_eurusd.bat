@echo off
setlocal

title automation-tool - %~nx0
echo.
echo ============================================================
echo Running: %~nx0
echo CWD    : %cd%
echo Args   : %*
echo ============================================================

REM EURUSD: capture charts -> analyze OpenAI; MT5 chỉ dry-run (không lệnh thật)
REM Mặc định headless (tiết kiệm RAM). Nếu cần debug UI: thêm --headed vào lệnh capture bên dưới.
REM Browser: chạy browser_up.bat trước.
cd /d "%~dp0"

if not exist "logs" mkdir "logs"

call ".venv\Scripts\activate.bat"
if errorlevel 1 (
  echo ERROR: Failed to activate virtual environment. See logs\eurusd_capture_analyze.log
  echo [%date% %time%] ERROR: venv>> "logs\eurusd_capture_analyze.log"
  exit /b 1
)

echo [%date% %time%] capture --main-symbol EURUSD
echo [%date% %time%] capture --main-symbol EURUSD>> "logs\eurusd_capture_analyze.log"
coinmap-automation capture --main-symbol EURUSD >> "logs\eurusd_capture_analyze.log" 2>&1
if errorlevel 1 (
  echo ERROR: capture failed code %ERRORLEVEL%. Log: logs\eurusd_capture_analyze.log
  echo [%date% %time%] ERROR capture %ERRORLEVEL%>> "logs\eurusd_capture_analyze.log"
  exit /b %ERRORLEVEL%
)

echo [%date% %time%] analyze --main-symbol EURUSD --mt5-dry-run --telegram-detail-chat-id -1003344625474
echo [%date% %time%] analyze --main-symbol EURUSD --mt5-dry-run --telegram-detail-chat-id -1003344625474>> "logs\eurusd_capture_analyze.log"
coinmap-automation analyze --main-symbol EURUSD --mt5-dry-run --telegram-detail-chat-id -1003344625474 >> "logs\eurusd_capture_analyze.log" 2>&1
set "EXIT_CODE=%ERRORLEVEL%"
echo [%date% %time%] Finished exit code %EXIT_CODE%>> "logs\eurusd_capture_analyze.log"

if %EXIT_CODE% neq 0 (
  echo ERROR: analyze failed code %EXIT_CODE%. Log: logs\eurusd_capture_analyze.log
) else (
  echo OK. Full log: logs\eurusd_capture_analyze.log
)
exit /b %EXIT_CODE%
