@echo off
setlocal

title automation-tool - %~nx0
echo.
echo ============================================================
echo Running: %~nx0
echo CWD    : %cd%
echo Args   : (hidden)
echo ============================================================

REM Login MT5 account via automation-tool CLI (Windows only).
REM Server/login are fixed for this account; password is NOT hardcoded.
REM Usage:
REM   run_mt5_login_exness_trial8.bat "<PASSWORD>"
REM Or set env var MT5_PASSWORD before running.

cd /d "%~dp0"
set "LOG_DIR=%~dp0logs"
if not exist "%LOG_DIR%" mkdir "%LOG_DIR%"
set "LOG_FILE=%LOG_DIR%\%~n0.log"
echo [%date% %time%] INFO: Logging to "%LOG_FILE%"
>> "%LOG_FILE%" echo.
>> "%LOG_FILE%" echo ============================================================
>> "%LOG_FILE%" echo [%date% %time%] Running: %~nx0
>> "%LOG_FILE%" echo CWD    : %cd%
>> "%LOG_FILE%" echo Args   : (hidden)
>> "%LOG_FILE%" echo ============================================================

REM Activate venv if present (recommended)
if exist ".venv\Scripts\activate.bat" (
  call ".venv\Scripts\activate.bat" >> "%LOG_FILE%" 2>&1
)

set "MT5_SERVER=Exness-MT5Trial8"
set "MT5_LOGIN=279566694"

set "PW=%~1"
if not "%PW%"=="" goto :have_pw
if not "%MT5_PASSWORD%"=="" (
  set "PW=%MT5_PASSWORD%"
  goto :have_pw
)

echo Enter MT5 password (will be visible as you type):
set /p "PW=> "

:have_pw

echo [%date% %time%] INFO: Starting mt5-login (server=%MT5_SERVER% login=%MT5_LOGIN%)
>> "%LOG_FILE%" echo [%date% %time%] INFO: Starting mt5-login (server=%MT5_SERVER% login=%MT5_LOGIN%)

coinmap-automation mt5-login --server "%MT5_SERVER%" --login %MT5_LOGIN% --password "%PW%" >> "%LOG_FILE%" 2>&1
set "EXIT_CODE=%ERRORLEVEL%"

echo [%date% %time%] INFO: Finished with exit code %EXIT_CODE%
>> "%LOG_FILE%" echo [%date% %time%] INFO: Finished with exit code %EXIT_CODE%
exit /b %EXIT_CODE%

