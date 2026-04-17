@echo off
setlocal EnableDelayedExpansion

REM Dừng cửa sổ run_daemon.bat (title khớp) và/hoặc python đang chạy tv-watchlist-daemon; chờ process hết (tối đa 60s) rồi force kill nếu cần.
REM Idempotent: không có daemon thì thoát 0.

cd /d "%~dp0"
if not exist "logs" mkdir "logs"
set "LOG=logs\daemon_recycle.log"
set /a MAX_WAIT=60

echo [%date% %time%] INFO: stop_daemon_wait begin>> "%LOG%"

REM 1) Theo title CMD giống run_daemon.bat (dòng title)
taskkill /FI "WINDOWTITLE eq automation-tool - run_daemon.bat" /T 2>nul

REM 2) Fallback: python.exe có chuỗi tv-watchlist-daemon trong command line
for /f "tokens=2 delims==" %%p in ('wmic process where "name='python.exe' and CommandLine like '%%tv-watchlist-daemon%%'" get ProcessId /value 2^>nul ^| findstr ProcessId') do (
  taskkill /PID %%p /T 2>nul
)

set /a WAITED=0
:waitloop
set "FOUND="
for /f "tokens=2 delims==" %%p in ('wmic process where "name='python.exe' and CommandLine like '%%tv-watchlist-daemon%%'" get ProcessId /value 2^>nul ^| findstr ProcessId') do set FOUND=1
if not defined FOUND goto waitdone
set /a WAITED+=1
if !WAITED! GTR !MAX_WAIT! (
  echo [%date% %time%] WARN: still running after !MAX_WAIT!s, force kill>> "%LOG%"
  for /f "tokens=2 delims==" %%p in ('wmic process where "name='python.exe' and CommandLine like '%%tv-watchlist-daemon%%'" get ProcessId /value 2^>nul ^| findstr ProcessId') do (
    taskkill /PID %%p /T /F 2>nul
  )
  goto waitdone
)
timeout /t 1 /nobreak >nul
goto waitloop

:waitdone
echo [%date% %time%] INFO: stop_daemon_wait end waited=!WAITED!>> "%LOG%"
exit /b 0
