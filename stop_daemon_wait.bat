@echo off
setlocal EnableDelayedExpansion

REM Dừng cửa sổ run_daemon.bat (title khớp) và/hoặc python đang chạy tv-watchlist-daemon; chờ process hết (tối đa 60s) rồi force kill nếu cần.
REM Idempotent: không có daemon thì thoát 0.

cd /d "%~dp0"
set "LOG_DIR=%~dp0logs"
if not exist "%LOG_DIR%" mkdir "%LOG_DIR%"
set "LOG_FILE=%LOG_DIR%\%~n0.log"
set /a MAX_WAIT=60

echo [%date% %time%] INFO: stop_daemon_wait begin
echo [%date% %time%] INFO: Logging to "%LOG_FILE%"
>> "%LOG_FILE%" echo.
>> "%LOG_FILE%" echo ============================================================
>> "%LOG_FILE%" echo [%date% %time%] Running: %~nx0
>> "%LOG_FILE%" echo CWD    : %cd%
>> "%LOG_FILE%" echo Args   : %*
>> "%LOG_FILE%" echo ============================================================
>> "%LOG_FILE%" echo [%date% %time%] INFO: stop_daemon_wait begin

REM 1) Theo title CMD giống run_daemon.bat (dòng title)
taskkill /FI "WINDOWTITLE eq automation-tool - run_daemon.bat" /T >> "%LOG_FILE%" 2>&1

REM 2) Fallback: python.exe có chuỗi tv-watchlist-daemon trong command line
for /f "tokens=2 delims==" %%p in ('wmic process where "name='python.exe' and CommandLine like '%%tv-watchlist-daemon%%'" get ProcessId /value 2^>nul ^| findstr ProcessId') do (
  taskkill /PID %%p /T >> "%LOG_FILE%" 2>&1
)

set /a WAITED=0
:waitloop
set "FOUND="
for /f "tokens=2 delims==" %%p in ('wmic process where "name='python.exe' and CommandLine like '%%tv-watchlist-daemon%%'" get ProcessId /value 2^>nul ^| findstr ProcessId') do set FOUND=1
if not defined FOUND goto waitdone
set /a WAITED+=1
if !WAITED! GTR !MAX_WAIT! (
  echo [%date% %time%] WARN: still running after !MAX_WAIT!s, force kill
  >> "%LOG_FILE%" echo [%date% %time%] WARN: still running after !MAX_WAIT!s, force kill
  for /f "tokens=2 delims==" %%p in ('wmic process where "name='python.exe' and CommandLine like '%%tv-watchlist-daemon%%'" get ProcessId /value 2^>nul ^| findstr ProcessId') do (
    taskkill /PID %%p /T /F >> "%LOG_FILE%" 2>&1
  )
  goto waitdone
)
timeout /t 1 /nobreak >nul
goto waitloop

:waitdone
echo [%date% %time%] INFO: stop_daemon_wait end waited=!WAITED!
>> "%LOG_FILE%" echo [%date% %time%] INFO: stop_daemon_wait end waited=!WAITED!
exit /b 0
