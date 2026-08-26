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

REM Activate virtual environment
call ".venv\Scripts\activate.bat" >> "%LOG_FILE%" 2>&1
if errorlevel 1 (
  echo [%date% %time%] ERROR: Failed to activate virtual environment.
  >> "%LOG_FILE%" echo [%date% %time%] ERROR: Failed to activate virtual environment.
  exit /b 1
)

REM Full pipeline: GoCharting footprint; `all` uses all-2 vector store (no second all-2 flow)
echo [%date% %time%] INFO: Starting coinmap-automation all --gocharting
>> "%LOG_FILE%" echo [%date% %time%] INFO: Starting coinmap-automation all --gocharting
coinmap-automation all --gocharting %* >> "%LOG_FILE%" 2>&1
set "EXIT_CODE=%ERRORLEVEL%"
echo [%date% %time%] INFO: Finished with exit code %EXIT_CODE%
>> "%LOG_FILE%" echo [%date% %time%] INFO: Finished with exit code %EXIT_CODE%

exit /b %EXIT_CODE%
