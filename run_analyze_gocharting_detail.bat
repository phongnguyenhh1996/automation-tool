@echo off
setlocal

title automation-tool - %~nx0
echo.
echo ============================================================
echo Running: %~nx0
echo CWD    : %cd%
echo Args   : %*
echo ============================================================

REM GoCharting detail footprint -^> JSON (M5 + M15, một request gpt-5.4).
REM Can detail PNG da co trong data\XAUUSD\charts\ (stamp moi nhat).
REM Output: m5_GC1!_footprint.json, m15_GC1!_footprint.json
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

echo [%date% %time%] INFO: Starting coinmap-automation analyze-gocharting-detail
>> "%LOG_FILE%" echo [%date% %time%] INFO: Starting coinmap-automation analyze-gocharting-detail
coinmap-automation analyze-gocharting-detail --main-symbol XAUUSD %* >> "%LOG_FILE%" 2>&1
set "EXIT_CODE=%ERRORLEVEL%"
echo [%date% %time%] INFO: analyze-gocharting-detail finished with exit code %EXIT_CODE%
>> "%LOG_FILE%" echo [%date% %time%] INFO: analyze-gocharting-detail finished with exit code %EXIT_CODE%

exit /b %EXIT_CODE%
