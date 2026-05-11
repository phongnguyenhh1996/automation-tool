@echo off
setlocal

title automation-tool - %~nx0
echo.
echo ============================================================
echo Running: %~nx0
echo CWD    : %cd%
echo Args   : %*
echo ============================================================

REM Khởi động browser service (Playwright CDP) — chạy riêng trước các task capture/daemon/telegram.
cd /d "%~dp0"

set "LOG_DIR=%~dp0logs"
set "LOG_FILE=%LOG_DIR%\browser_up_errors.log"
set "SERVICE_LOG_FILE=%LOG_DIR%\browser_service.log"
if not exist "%LOG_DIR%" mkdir "%LOG_DIR%"
set "BROWSER_SERVICE_LOG_FILE=%SERVICE_LOG_FILE%"

call ".venv\Scripts\activate.bat" 2>> "%LOG_FILE%"
if errorlevel 1 (
  echo [%date% %time%] ERROR: Failed to activate virtual environment.
  >> "%LOG_FILE%" echo [%date% %time%] ERROR: Failed to activate virtual environment.
  exit /b 1
)

echo [%date% %time%] INFO: browser up
echo Logs: "%LOG_FILE%" / "%SERVICE_LOG_FILE%"
coinmap-automation browser up >> "%LOG_FILE%" 2>&1
set "EXIT_CODE=%ERRORLEVEL%"
if %EXIT_CODE% neq 0 (
  echo [%date% %time%] ERROR: browser up failed code %EXIT_CODE%
  >> "%LOG_FILE%" echo [%date% %time%] ERROR: browser up failed code %EXIT_CODE%
  echo Check logs:
  echo   "%LOG_FILE%"
  echo   "%SERVICE_LOG_FILE%"
  if /i not "%BROWSER_UP_PAUSE_ON_ERROR%"=="0" pause
) else (
  echo [%date% %time%] INFO: browser up OK
)
exit /b %EXIT_CODE%
