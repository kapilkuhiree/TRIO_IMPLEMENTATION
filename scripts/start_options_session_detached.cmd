@echo off
cd /d "C:\Users\Kapil Kuhire\Downloads\TRIO_IMPLEMENTATION-main"
set TRIO_MARKET_DATA_PROVIDER=zerodha
set PAPER_TRADING=true
set TRADING_MODE=options_intraday
set TRIO_OPTIONS_SCAN_UNDERLYINGS=NIFTY
set TRIO_PAPER_LONG_STRUCTURES_ONLY=1
set TRIO_PAPER_ALL_STRATEGIES=0
"C:\Python314\python.exe" scripts/session.py --trading-mode options_intraday --interval 10 >> "session_20261125_detached_out.log" 2>> "session_20261125_detached_err.log"
