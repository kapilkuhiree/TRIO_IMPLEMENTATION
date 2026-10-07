"""
TRIO — real-data evaluation harness (temporary diagnostic script).
Author: Kapil Kuhire <kapilkuhire89@gmail.com>

Runs the backtester over a set of symbols/timeframes and dumps metrics to JSON
so results can be inspected without terminal formatting noise.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.backtester import run_backtest
from src.utils import setup_logging

setup_logging("ERROR")  # silence INFO noise; we want results only

CASES = [
    ("RELIANCE.NS", "1d", "5y"),
    ("TCS.NS", "1d", "5y"),
    ("INFY.NS", "1d", "5y"),
    ("HDFCBANK.NS", "1d", "5y"),
    ("ICICIBANK.NS", "1d", "5y"),
    ("SBIN.NS", "1d", "5y"),
    ("ITC.NS", "1d", "5y"),
    ("LT.NS", "1d", "5y"),
    ("AXISBANK.NS", "1d", "5y"),
    ("MARUTI.NS", "1d", "5y"),
    ("HINDUNILVR.NS", "1d", "5y"),
    ("SUNPHARMA.NS", "1d", "5y"),
    ("KOTAKBANK.NS", "1d", "5y"),
    ("WIPRO.NS", "1d", "5y"),
]

out = []
for symbol, tf, period in CASES:
    try:
        r = run_backtest(symbol=symbol, timeframe=tf, period=period)
        reasons = {}
        for t in r.trades:
            reasons[t.exit_reason] = reasons.get(t.exit_reason, 0) + 1
        out.append({
            "symbol": r.symbol,
            "timeframe": r.timeframe,
            "period": r.period,
            "trades": r.total_trades,
            "wins": r.wins,
            "losses": r.losses,
            "win_rate_pct": r.win_rate,
            "profit_factor": r.profit_factor,
            "total_return_pct": r.total_return_pct,
            "max_dd_pct": r.max_drawdown_pct,
            "sharpe": r.sharpe_ratio,
            "avg_rr": r.avg_risk_reward,
            "exit_reasons": reasons,
        })
    except Exception as exc:
        out.append({"symbol": symbol, "error": f"{type(exc).__name__}: {exc}"})

target = Path(__file__).resolve().parent.parent / "backtest_results.json"
target.write_text(json.dumps(out, indent=2), encoding="utf-8")
print("WROTE", target)
for row in out:
    print(row)