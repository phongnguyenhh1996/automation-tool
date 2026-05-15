@echo off
setlocal

title automation-tool - %~nx0
echo.
echo ============================================================
echo Running: %~nx0
echo CWD    : %cd%
echo Args   : %*
echo ============================================================

REM Multi-symbol: Coinmap(all) -> TradingView(all) -> OpenAI parallel analyze
REM Symbols: EURUSD, USDJPY
REM Browser: chạy browser_up.bat trước.
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

echo [%date% %time%] INFO: Starting capture-many (EURUSD,USDJPY)
>> "%LOG_FILE%" echo [%date% %time%] INFO: Starting capture-many (EURUSD,USDJPY)
coinmap-automation capture-many --use-service --symbols EURUSD,USDJPY >> "%LOG_FILE%" 2>&1
if errorlevel 1 (
  echo [%date% %time%] ERROR: capture-many failed code %ERRORLEVEL%
  >> "%LOG_FILE%" echo [%date% %time%] ERROR: capture-many failed code %ERRORLEVEL%
  exit /b %ERRORLEVEL%
)

echo [%date% %time%] INFO: Starting analyze-many (EURUSD,USDJPY) parallel=1 (MT5 disabled by default)
>> "%LOG_FILE%" echo [%date% %time%] INFO: Starting analyze-many (EURUSD,USDJPY) parallel=1 (MT5 disabled by default)
coinmap-automation analyze-many --symbols EURUSD,USDJPY --parallel 1 >> "%LOG_FILE%" 2>&1
set "EXIT_CODE=%ERRORLEVEL%"
echo [%date% %time%] INFO: Finished with exit code %EXIT_CODE%
>> "%LOG_FILE%" echo [%date% %time%] INFO: Finished with exit code %EXIT_CODE%

exit /b %EXIT_CODE%

