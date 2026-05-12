@echo off
setlocal

title automation-tool - %~nx0
echo.
echo ============================================================
echo Running: %~nx0
echo CWD    : %cd%
echo Args   : %*
echo ============================================================

REM Ensure script runs from project root
cd /d "%~dp0"

REM Activate virtual environment
call ".venv\Scripts\activate.bat"
if errorlevel 1 (
  echo [%date% %time%] ERROR: Failed to activate virtual environment.
  exit /b 1
)

REM Run full pipeline (headless by default to save memory; add --headed manually if needed)
echo [%date% %time%] INFO: Starting coinmap-automation all
coinmap-automation all
set "EXIT_CODE=%ERRORLEVEL%"
echo [%date% %time%] INFO: Finished with exit code %EXIT_CODE%

exit /b %EXIT_CODE%
