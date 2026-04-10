@echo off
setlocal

title automation-tool - %~nx0
echo.
echo ============================================================
echo Running: %~nx0
echo CWD    : %cd%
echo Args   : %*
echo ============================================================

REM Dừng browser service (đối chiếu với browser_up.bat).
cd /d "%~dp0"

if not exist "logs" mkdir "logs"

call ".venv\Scripts\activate.bat"
if errorlevel 1 (
  echo [%date% %time%] ERROR: Failed to activate virtual environment.>> "logs\browser_service.log"
  exit /b 1
)

echo [%date% %time%] INFO: browser down>> "logs\browser_service.log"
coinmap-automation browser down >> "logs\browser_service.log" 2>&1
set "EXIT_CODE=%ERRORLEVEL%"
if %EXIT_CODE% neq 0 (
  echo [%date% %time%] ERROR: browser down failed code %EXIT_CODE%>> "logs\browser_service.log"
) else (
  echo [%date% %time%] INFO: browser down OK>> "logs\browser_service.log"
)
exit /b %EXIT_CODE%
