@echo off
setlocal enabledelayedexpansion
title TRIO Options Intraday - 10 Second Scan (PAPER)

REM ============================================================
REM  Logs are ALWAYS dated with TODAY's date (YYYYMMDD).
REM  No date argument is accepted on purpose: strict options mode
REM  only trades the current IST session, so an old date would
REM  silently switch the session to management-only.
REM ============================================================

cd /d "C:\Users\Kapil Kuhire\Downloads\TRIO_IMPLEMENTATION-main"

for /f %%A in ('powershell -NoProfile -Command "Get-Date -Format 'yyyyMMdd'"') do set "LOG_DATE=%%A"
set "OUT_LOG=session_%LOG_DATE%_out.log"
set "ERR_LOG=session_%LOG_DATE%_err.log"

echo.
echo ==============================================
echo   TRIO Options Intraday - PAPER mode
echo ==============================================
echo   Today (date-stamped): %LOG_DATE%
echo   Out log: %OUT_LOG%
echo   Err log: %ERR_LOG%
echo.

REM --- Do not start a second session on top of a running one ---
tasklist /fi "imagename eq python.exe" 2>nul | find /i "python.exe" >nul
if not errorlevel 1 (
    echo [ERROR] A python.exe session is already running.
    echo         Close it first ^(Ctrl+C^) or run: taskkill /f /im python.exe
    echo.
    pause
    exit /b 1
)

REM --- Reset daily counters only when the calendar day changed ---
set "STATE_PATH=%LOCALAPPDATA%\TRIO\options_intraday\state.json"
if exist "%STATE_PATH%" (
    set "STORED_DATE="
    for /f "tokens=2 delims=:" %%A in ('findstr /c:"session_date" "%STATE_PATH%"') do (
        set "RAW=%%A"
        set "RAW=!RAW: =!"
        set "RAW=!RAW:,=!"
        set "RAW=!RAW:"=!"
        set "STORED_DATE=!RAW:-=!"
    )
    if "!STORED_DATE!"=="%LOG_DATE%" (
        echo   State: same day ^(%LOG_DATE%^) - keeping daily_entries and positions.
    ) else (
        echo   State: day changed ^(!STORED_DATE! -^> %LOG_DATE%^) - resetting daily counters.
        powershell -NoProfile -Command "$p='%STATE_PATH%'; $j=Get-Content $p -Raw | ConvertFrom-Json; $j.daily_entries=0; $j.session_date='%LOG_DATE%'.Insert(4,'-').Insert(7,'-'); $j.closed=@(); $j | ConvertTo-Json -Depth 10 | Set-Content $p -Encoding utf8"
        if errorlevel 1 (
            echo   [WARN] state.json reset failed - is a session still running?
        ) else (
            echo   State: daily_entries = 0, session_date = %LOG_DATE%
        )
    )
) else (
    echo   State: no state.json - starting fresh.
)

echo.
echo   Starting python session (entries ENABLED - no --no-plan)...
echo   Press Ctrl+C to stop. Hard flat at 15:10 IST.
echo.

REM Append (>>) so a restart never erases today's earlier entries/exits.
echo. >> "%OUT_LOG%"
echo ============================================================ >> "%OUT_LOG%"
echo   SESSION START  %DATE% %TIME% >> "%OUT_LOG%"
echo ============================================================ >> "%OUT_LOG%"
echo. >> "%ERR_LOG%"
echo ============================================================ >> "%ERR_LOG%"
echo   SESSION START  %DATE% %TIME% >> "%ERR_LOG%"
echo ============================================================ >> "%ERR_LOG%"

python scripts\session.py --trading-mode options_intraday --interval 10 >> "%OUT_LOG%" 2>> "%ERR_LOG%"

echo.
echo ==============================================
echo   Session ended
echo ==============================================
echo   Out log: %OUT_LOG%
echo   Err log: %ERR_LOG%
echo   Trade log: output\trade_log.jsonl (preserved)
echo.
pause
