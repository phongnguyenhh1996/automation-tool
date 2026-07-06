@echo off
setlocal EnableDelayedExpansion

title automation-tool - %~nx0
echo.
echo ============================================================
echo Running: %~nx0
echo CWD    : %cd%
echo Args   : %*
echo ============================================================

REM Scalp footprint VPS executor: listen Telegram SCALP_EXEC -> MT5 MARKET.
REM Can chay sau khi MT5 terminal da login (Task Scheduler "At log on").
REM Cau hinh chinh: .env hoac block CONFIG ben duoi.
REM   TELEGRAM_BOT_TOKEN
REM   SCALP_EXEC_ACCOUNT_IDS=id trong accounts.json (vd: main)
REM   MT5_ACCOUNTS_JSON=duong dan accounts.json
REM SL/TP: lay bid/ask MT5 live (XAUUSD/XAUUSDm/XAUUSDc), +-4 gia mac dinh.

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

REM --- CONFIG (ghi de .env neu can) ---------------------------------
REM set "SCALP_EXEC_DRY_RUN=1"
set "SCALP_EXEC_DRY_RUN=0"
set "SCALP_EXEC_LOT=0.01"
set "SCALP_EXEC_SL_POINTS=4"
set "SCALP_EXEC_TP_POINTS=4"
set "SCALP_EXEC_ACCOUNT_IDS=acc_secondary2"
REM Telegram channel scalp footprint (hardcoded in telegram_executor.py): -1004297700919
REM set "MT5_ACCOUNTS_JSON=%~dp0config\accounts.json"
REM set "SCALP_EXEC_PATTERNS="
REM -------------------------------------------------------------------

if exist ".venv\Scripts\activate.bat" (
  call ".venv\Scripts\activate.bat" >> "%LOG_FILE%" 2>&1
  if errorlevel 1 (
    echo [%date% %time%] ERROR: Failed to activate virtual environment.
    >> "%LOG_FILE%" echo [%date% %time%] ERROR: Failed to activate virtual environment.
    exit /b 1
  )
)

set "PY=python"
where python >nul 2>&1
if errorlevel 1 set "PY=py -3"

set "EXEC_SCRIPT=%~dp0scripts\scalp_footprint\telegram_executor.py"
if not exist "%EXEC_SCRIPT%" (
  echo [%date% %time%] ERROR: Missing %EXEC_SCRIPT%
  >> "%LOG_FILE%" echo [%date% %time%] ERROR: Missing %EXEC_SCRIPT%
  exit /b 1
)

set "DRY_FLAG=--no-dry-run"
if "%SCALP_EXEC_DRY_RUN%"=="1" set "DRY_FLAG=--dry-run"

echo [%date% %time%] INFO: Starting scalp footprint executor %DRY_FLAG% lot=%SCALP_EXEC_LOT% sl/tp=%SCALP_EXEC_SL_POINTS%/%SCALP_EXEC_TP_POINTS%
>> "%LOG_FILE%" echo [%date% %time%] INFO: Starting scalp footprint executor %DRY_FLAG% lot=%SCALP_EXEC_LOT% sl/tp=%SCALP_EXEC_SL_POINTS%/%SCALP_EXEC_TP_POINTS%
if defined SCALP_EXEC_ACCOUNT_IDS (
  echo [%date% %time%] INFO: SCALP_EXEC_ACCOUNT_IDS=%SCALP_EXEC_ACCOUNT_IDS%
  >> "%LOG_FILE%" echo [%date% %time%] INFO: SCALP_EXEC_ACCOUNT_IDS=%SCALP_EXEC_ACCOUNT_IDS%
)
if defined MT5_ACCOUNTS_JSON (
  echo [%date% %time%] INFO: MT5_ACCOUNTS_JSON=%MT5_ACCOUNTS_JSON%
  >> "%LOG_FILE%" echo [%date% %time%] INFO: MT5_ACCOUNTS_JSON=%MT5_ACCOUNTS_JSON%
)

"%PY%" "%EXEC_SCRIPT%" %DRY_FLAG% -v %*
set "EXIT_CODE=%ERRORLEVEL%"
echo [%date% %time%] INFO: Finished with exit code %EXIT_CODE%
>> "%LOG_FILE%" echo [%date% %time%] INFO: Finished with exit code %EXIT_CODE%

exit /b %EXIT_CODE%
