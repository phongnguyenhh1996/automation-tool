@echo off
setlocal

REM Khởi động browser service (Playwright CDP) — chạy riêng trước các task capture/daemon/telegram.
cd /d "%~dp0"

if not exist "logs" mkdir "logs"

call ".venv\Scripts\activate.bat"
if errorlevel 1 (
  echo [%date% %time%] ERROR: Failed to activate virtual environment.>> "logs\browser_service.log"
  exit /b 1
)

echo [%date% %time%] INFO: browser up>> "logs\browser_service.log"
coinmap-automation browser up >> "logs\browser_service.log" 2>&1
set "EXIT_CODE=%ERRORLEVEL%"
if %EXIT_CODE% neq 0 (
  echo [%date% %time%] ERROR: browser up failed code %EXIT_CODE%>> "logs\browser_service.log"
) else (
  echo [%date% %time%] INFO: browser up OK>> "logs\browser_service.log"
)
exit /b %EXIT_CODE%
