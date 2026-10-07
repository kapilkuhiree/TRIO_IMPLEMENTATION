# NEXT SESSION START HERE
If this model just woke up: read NEXT_SESSION.md first (one short file),
then verify with: cd to the TRIO folder and `python -m pytest tests -q`.
Then wait for the user's paper-trading log. Do nothing else until it arrives.

---

TRIO — Handoff for next model/session
======================================
Read this first. Keep it short and token-light.

CONTEXT SNAPSHOT
----------------
- Repo: C:\Users\Kapil Kuhire\Downloads\TRIO
- Status: draft code exists; tests pass; we are now working against that draft.
- Latest state: Phase 0 complete (rules + shared todo.txt). Approval received to proceed.
- Tests: python -m pytest tests -> 37 passed (2026-10-04)
- Compilation: python -m compileall src main.py -> OK
- RSI fix: no down bars => 100, no up bars => 0 (was returning None)

HOW WE WORK (non-negotiable)
----------------------------
1. Only do the next unchecked step in todo.txt. Never dump the whole project.
2. One module per response after architecture approval. For any change: diff/changed functions only.
3. After each approved phase, update PROJECT_STATE.md and todo.txt.
4. API keys only from env; no live orders unless explicitly enabled; default paper/signal-only.
5. Treat screenshot-extracted values as hints; always cross-check against live data.
6. Every trade signal must include: symbol, action, entry, stop-loss, target, position_size, risk_amount, confidence, reasoning.

KEY FILES
---------
- todo.txt           current "what/how/done/next". Keep this updated.
- PROJECT_STATE.md   exists? check: if missing, create at end of Phase 1. If present, append.
- src/*              draft modules (valid, with type hints/docstrings/logging). Safe to refine in place.
- tests/*            3 test files cover indicators, signal_engine, risk_manager. Need more for cross_checker, paper broker, backtester.
- config/config.yaml has sensible defaults.
- .env.example shows required keys.

NEXT ACTION (as of last update)
-------------------------------
See todo.txt under "NEXT ACTION": STEP 1 is done conceptually via approval, but we need to write PROJECT_STATE.md for Phase 0 and pick the actual next phase. 

If starting fresh: read todo.txt first, then verify tests still pass, then do the smallest next item. 

DECISIONS MADE RECENTLY
-----------------------
- Keep draft, don't rewrite from zero.
- Added MODEL_HANDOFF.md to avoid reloading full context.
- Risk halt: when position limit is hit, halt stays active until reset_risk_state/reset_daily. Closing positions should not auto-clear halt by itself (that's intentional for safety). Test reflects this.
- Backtest exits on SL/TP and also closes open trade at end.

QUICK COMMANDS TO RESUME
------------------------
cd "C:\Users\Kapil Kuhire\Downloads\TRIO"
python -m pytest tests -q
python -m compileall src main.py
git status (if git ever used)
