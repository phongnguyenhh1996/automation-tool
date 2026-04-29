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

call ".venv\Scripts\activate.bat"
if errorlevel 1 (
  echo [%date% %time%] ERROR: Failed to activate virtual environment.
  exit /b 1
)

echo [%date% %time%] INFO: browser down
coinmap-automation browser down
set "EXIT_CODE=%ERRORLEVEL%"
if %EXIT_CODE% neq 0 (
  echo [%date% %time%] ERROR: browser down failed code %EXIT_CODE%
) else (
  echo [%date% %time%] INFO: browser down OK
)
exit /b %EXIT_CODE%
