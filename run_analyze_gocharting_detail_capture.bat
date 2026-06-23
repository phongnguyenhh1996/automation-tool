@echo off
setlocal

title automation-tool - %~nx0
echo.
echo ============================================================
echo Running: %~nx0
echo CWD    : %cd%
echo Args   : %*
echo ============================================================

REM Capture GoCharting detail M5+M15 roi trich xuat JSON footprint (mot request OpenAI gpt-5.4).
REM Chay browser_up.bat truoc neu dung --use-service.
REM Output: data\XAUUSD\charts\m5_GC1!_footprint.json, m15_GC1!_footprint.json
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

echo [%date% %time%] INFO: Starting coinmap-automation analyze-gocharting-detail --capture --headed --use-service
>> "%LOG_FILE%" echo [%date% %time%] INFO: Starting coinmap-automation analyze-gocharting-detail --capture --headed --use-service
coinmap-automation analyze-gocharting-detail --capture --headed --use-service %* >> "%LOG_FILE%" 2>&1
set "EXIT_CODE=%ERRORLEVEL%"
echo [%date% %time%] INFO: analyze-gocharting-detail capture finished with exit code %EXIT_CODE%
>> "%LOG_FILE%" echo [%date% %time%] INFO: analyze-gocharting-detail capture finished with exit code %EXIT_CODE%

exit /b %EXIT_CODE%
