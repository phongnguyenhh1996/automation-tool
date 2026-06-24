@echo off
setlocal

title automation-tool - %~nx0
echo.
echo ============================================================
echo Running: %~nx0
echo CWD    : %cd%
echo Args   : %*
echo ============================================================

REM OCR batch PNG footprint trong footprint_images -> footprint_bid_ask_*.json
REM Chi OCR cac moc thoi gian chua co trong JSON; giua moi lan goi OCR.space cach 5s.
REM Can OCR_SPACE_API_KEY trong .env (ocr.space)
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

set "AUTOMATION_MAIN_SYMBOL=XAUUSD"

call ".venv\Scripts\activate.bat" >> "%LOG_FILE%" 2>&1
if errorlevel 1 (
  echo [%date% %time%] ERROR: Failed to activate virtual environment.
  >> "%LOG_FILE%" echo [%date% %time%] ERROR: Failed to activate virtual environment.
  exit /b 1
)

echo [%date% %time%] INFO: Starting coinmap-automation footprint-gocharting-ocr
>> "%LOG_FILE%" echo [%date% %time%] INFO: Starting coinmap-automation footprint-gocharting-ocr
coinmap-automation footprint-gocharting-ocr %* >> "%LOG_FILE%" 2>&1
set "EXIT_CODE=%ERRORLEVEL%"
echo [%date% %time%] INFO: footprint-gocharting-ocr finished with exit code %EXIT_CODE%
>> "%LOG_FILE%" echo [%date% %time%] INFO: footprint-gocharting-ocr finished with exit code %EXIT_CODE%

exit /b %EXIT_CODE%
