# Handoff: paste this into a new chat tomorrow

## Who we are
- Folder: `C:\Users\Kapil Kuhire\Downloads\TRIO`
- Strategy: pullback entries in an uptrend (close > 200SMA, close < 50SMA, MACD turned up).
- Stop: below the recent 10-bar swing low. Target: 1.5R. No trailing stop.
- Evidence: this setup passed out-of-sample testing (`scripts/validate_oos.py`:
  ~42% win rate, profit factor ~1.25 on 15 NSE large caps, 5y daily). High win-rate
  settings collapsed out-of-sample and were rejected.

## Status (verified 2026-10-04)
- 64 unit tests pass. `python -m pytest tests -q` → all green.
- App + paper trader compile and run paper mode cleanly (test with 1 iteration first).
- Paper broker has mark-to-market (`update_position`) and session close-out
  (`close_position`), and the paper loop closes positions on stop/target hits.
- `config.yaml` has a `forward_test` section (NSE 09:20–15:15 IST, 15m scans).

## Restart phrase (copy exactly)
"Trio resume: read C:\Users\Kapil Kuhire\Downloads\TRIO\MODEL_HANDOFF.md,
todo.txt and PROJECT_STATE.md, then run pytest to verify, and wait for the
day's paper-trading log."

## The daily loop
1. You run (market days, NSE hours):
   `python main.py scan --symbols RELIANCE.NS TCS.NS INFY.NS HDFCBANK.NS SBIN.NS --timeframe 15m`
   or the timed loop: `python main.py paper --symbols RELIANCE.NS TCS.NS INFY.NS --timeframe 15m --interval 900`
2. Paste the terminal output here.
3. I diff behaviour vs. the validated strategy and patch code. Nothing else.

## Rules for the next model
- Do not rewrite the strategy. Only fix bugs and small gaps the log proves.
- Do not chase an 80% win rate (evidence is in `scripts/validation_oos.json`).
- Return only diffs/changed functions, not whole files.
- After each change: rerun pytest, update todo.txt + PROJECT_STATE.md.
