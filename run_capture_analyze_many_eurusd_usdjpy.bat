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

if not exist "logs" mkdir "logs"

call ".venv\Scripts\activate.bat"
if errorlevel 1 (
  echo [%date% %time%] ERROR: Failed to activate virtual environment.>> "logs\capture_analyze_many_eurusd_usdjpy.log"
  exit /b 1
)

echo [%date% %time%] INFO: Starting capture-many (EURUSD,USDJPY)>> "logs\capture_analyze_many_eurusd_usdjpy.log"
coinmap-automation capture-many --use-service --symbols EURUSD,USDJPY >> "logs\capture_analyze_many_eurusd_usdjpy.log" 2>&1
if errorlevel 1 (
  echo [%date% %time%] ERROR: capture-many failed code %ERRORLEVEL%>> "logs\capture_analyze_many_eurusd_usdjpy.log"
  exit /b %ERRORLEVEL%
)

echo [%date% %time%] INFO: Starting analyze-many (EURUSD,USDJPY) parallel=2 (MT5 disabled by default)>> "logs\capture_analyze_many_eurusd_usdjpy.log"
coinmap-automation analyze-many --symbols EURUSD,USDJPY --parallel 2 >> "logs\capture_analyze_many_eurusd_usdjpy.log" 2>&1
set "EXIT_CODE=%ERRORLEVEL%"
echo [%date% %time%] INFO: Finished with exit code %EXIT_CODE%>> "logs\capture_analyze_many_eurusd_usdjpy.log"

exit /b %EXIT_CODE%

