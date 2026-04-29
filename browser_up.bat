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

call ".venv\Scripts\activate.bat"
if errorlevel 1 (
  echo [%date% %time%] ERROR: Failed to activate virtual environment.
  exit /b 1
)

echo [%date% %time%] INFO: browser up
coinmap-automation browser up
set "EXIT_CODE=%ERRORLEVEL%"
if %EXIT_CODE% neq 0 (
  echo [%date% %time%] ERROR: browser up failed code %EXIT_CODE%
) else (
  echo [%date% %time%] INFO: browser up OK
)
exit /b %EXIT_CODE%
