@echo off
rem Daily shadow runner: ORB + true VWAP on NIFTY futures.
rem NEVER places, sizes or blocks an order - ledger only.
rem Runs after the 15:10 hard flat so the session's bars are complete.
cd /d "C:\Users\Kapil Kuhire\Downloads\TRIO_IMPLEMENTATION-main"
for /f %%d in ('powershell -NoProfile -Command "Get-Date -Format yyyy-MM-dd"') do set "TODAY=%%d"
"C:\Python314\python.exe" scripts\shadow_orb_vwap.py --date %TODAY% >> "output\shadow_daily.log" 2>&1
set "SHADOW_RC=%ERRORLEVEL%"
"C:\Python314\python.exe" scripts\audit_live_vwap.py --date %TODAY% >> "output\shadow_daily.log" 2>&1
rem The audit exits 1 when the live book had no entries that day (expected on a
rem no-trade day). Only the shadow ledger decides the task result.
exit /b %SHADOW_RC%