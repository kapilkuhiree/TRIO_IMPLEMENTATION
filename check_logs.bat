@echo off
title TRIO — Scan Logs for NIFTY/BANKNIFTY Legs & Contracts

echo ==============================================
echo 📄 Analyzing most recent session logs...
echo ==============================================

cd C:\Users\Kapil Kuhire\Downloads\TRIO_IMPLEMENTATION-main

REM Find the latest error log
for /f "delims=" %%i in ('dir session_*_err.log /b /a-d /od') do set LATEST_LOG=%%i

if "%LATEST_LOG%"=="" (
    echo ❌ No logs found!
    pause
    exit /b
)

echo.
echo 🔎 Checking: %LATEST_LOG%
echo ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
echo 📋 CONTRACT SELECTIONS FOUND:
echo ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

REM Extract key data using findstr for contract selection filtering
echo.
findstr /c:"option_underlying" "%LATEST_LOG%" ^
  | findstr /c:"trading_symbol" ^
  | findstr /c:"position_id" ^
  | findstr /c:"profit_lock_stage" ^
  | sort

echo ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
echo.
echo 📌 Legend:
echo   • underlying: BANKNIFTY or NIFTY
echo   • trading_symbol: Specific contract leg (e.g. 54800PE)
echo   • profit_lock_stage: 1 = +10% Ladder active, 2 = +20% Ladder active
echo.
pause