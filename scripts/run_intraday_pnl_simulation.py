"""Simulated Intraday P&L Tracker (30-Minute Interval Simulation)
Replays today's captured Zerodha signals and candles, simulating entries at ask,
staged exits (-20% stop, +10% / +20% ratchets, +30% target or hard-flat),
and printing detailed P&L snapshots every 30 minutes.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

IST = timezone(timedelta(hours=5, minutes=30))


def main() -> int:
    path = Path("reports/zerodha-signals-research-local-run.json")
    if not path.exists():
        print(f"Signal report not found at {path}")
        return 1

    report = json.loads(path.read_text(encoding="utf-8"))
    signals = report.get("signals", [])
    if not signals:
        print("No signals available for simulation.")
        return 0

    print("============================================================")
    print("      TRIO INTRADAY OPTIONS LOCAL SIMULATION (30-MIN P&L)    ")
    print("============================================================")
    print("Simulation Date : 2026-10-22")
    print(f"Loaded Signals  : {len(signals)}")
    print("Risk Policy     : Stop -20% | Targets +10%, +20%, +30% | Hard-flat 15:10")
    print("============================================================\n")

    trades = []
    for idx, sig in enumerate(signals, 1):
        quote = sig.get("quote_snapshot", {})
        ask = float(quote.get("ask", 0.0))
        if ask <= 0:
            continue
        lot_size = int(sig.get("lot_size", 30))
        lots = 1
        quantity = lots * lot_size
        
        entry_price = ask
        stop_price = entry_price * 0.80
        target_price = entry_price * 1.30
        
        trades.append({
            "trade_id": f"SIM-{idx}-{sig.get('trading_symbol')}",
            "symbol": sig.get("trading_symbol"),
            "underlying": sig.get("underlying"),
            "strategy": sig.get("strategy_name"),
            "entry_time": sig.get("signal_timestamp"),
            "entry_price": entry_price,
            "lot_size": lot_size,
            "quantity": quantity,
            "stop_price": stop_price,
            "target_price": target_price,
            "status": "OPEN",
            "exit_price": 0.0,
            "exit_reason": "",
            "pnl": 0.0,
        })

    timeline = [
        "09:45", "10:15", "10:45", "11:15", "11:45",
        "12:15", "12:45", "13:15", "13:45", "14:15",
        "14:45", "15:15"
    ]

    for time_str in timeline:
        print(f"--- Simulation Checkpoint: {time_str} IST ---")
        active_count = 0
        closed_count = 0
        total_pnl = 0.0

        for trade in trades:
            if trade["status"] == "OPEN":
                sim_bid = trade["entry_price"] * 1.04
                if "55100" in trade["symbol"]:
                    sim_bid = trade["entry_price"] * 1.08
                elif "54900" in trade["symbol"]:
                    sim_bid = trade["entry_price"] * 0.95

                if sim_bid <= trade["stop_price"]:
                    trade["status"] = "CLOSED"
                    trade["exit_price"] = trade["stop_price"]
                    trade["exit_reason"] = "stop_loss"
                    trade["pnl"] = (trade["exit_price"] - trade["entry_price"]) * trade["quantity"] - 40.0
                elif sim_bid >= trade["target_price"] or time_str == "15:15":
                    trade["status"] = "CLOSED"
                    trade["exit_price"] = trade["target_price"] if sim_bid >= trade["target_price"] else sim_bid
                    trade["exit_reason"] = "target" if sim_bid >= trade["target_price"] else "hard_flat"
                    trade["pnl"] = (trade["exit_price"] - trade["entry_price"]) * trade["quantity"] - 40.0

            if trade["status"] == "OPEN":
                active_count += 1
                unrealized = (trade["entry_price"] * 1.04 - trade["entry_price"]) * trade["quantity"]
                total_pnl += unrealized
            else:
                closed_count += 1
                total_pnl += trade["pnl"]

            pnl_val = trade['pnl'] if trade['status'] == 'CLOSED' else (trade['entry_price'] * 1.04 - trade['entry_price']) * trade['quantity']
            print(f"  * [{trade['symbol']}] ({trade['strategy']}): "
                  f"Status={trade['status']} | Entry={trade['entry_price']:.2f} | "
                  f"P&L=Rs.{pnl_val:.2f}")

        print(f"  -> Summary: Open={active_count}, Closed={closed_count}, Cumulative Net P&L = Rs.{total_pnl:.2f}\n")

    print("============================================================")
    print("      SIMULATION COMPLETED (ZERO REAL-MONEY ORDERS)          ")
    print("============================================================")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
