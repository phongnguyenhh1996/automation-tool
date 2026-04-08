@echo off
setlocal

REM Login MT5 account via automation-tool CLI (Windows only).
REM Server/login are fixed for this account; password is NOT hardcoded.
REM Usage:
REM   run_mt5_login_exness_trial8.bat "<PASSWORD>"
REM Or set env var MT5_PASSWORD before running.

cd /d "%~dp0"

if not exist "logs" mkdir "logs"

REM Activate venv if present (recommended)
if exist ".venv\Scripts\activate.bat" (
  call ".venv\Scripts\activate.bat"
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

echo [%date% %time%] INFO: Starting mt5-login (server=%MT5_SERVER% login=%MT5_LOGIN%)>> "logs\mt5_login_exness_trial8.log"

coinmap-automation mt5-login --server "%MT5_SERVER%" --login %MT5_LOGIN% --password "%PW%" >> "logs\mt5_login_exness_trial8.log" 2>&1
set "EXIT_CODE=%ERRORLEVEL%"

echo [%date% %time%] INFO: Finished with exit code %EXIT_CODE%>> "logs\mt5_login_exness_trial8.log"
exit /b %EXIT_CODE%

