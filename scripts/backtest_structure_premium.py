"""Premium-proxy backtest for the long-straddle / long-strangle sleeves.

This is a READ-ONLY research replay.  It uses:

* real NIFTY 5-minute underlying candles from ``output/post_expiry_5m_*.pkl``;
* real long-volatility signal classes (same trigger chain as the live planner);
* real NIFTY option *candle closes* from ``SQLiteOptionArchive`` as the premium
  proxy for both legs.

It is deliberately labelled a proxy: the archive has almost no usable bid/ask
quotes for these structures, so entry/mark/exit use option candle closes
instead of the live runtime's executable ask/bid.  Costs are charged with the
same ``round_trip_cost`` helper used by the live paper adapters.

Usage (from repo root):
    python scripts/backtest_structure_premium.py
    python scripts/backtest_structure_premium.py --triggers orb
"""

from __future__ import annotations

import argparse
import csv
import json
import pickle
import sqlite3
import sys
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.options_pricing import round_trip_cost  # noqa: E402
from strategies.long_straddle_strategy import LongStraddleStrategy  # noqa: E402
from strategies.long_strangle_strategy import LongStrangleStrategy  # noqa: E402

IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc

DB = Path(r"C:\Users\Kapil Kuhire\AppData\Local\TRIO\options\options_archive.sqlite3")
PKL = ROOT / "output" / "post_expiry_5m_NIFTY_20260706_20261120.pkl"
OUT_JSON = ROOT / "output" / "structure_premium_backtest.json"
OUT_CSV = ROOT / "output" / "structure_premium_backtest_trades.csv"

LOT = 65
STEP = 50
STOP_FRACTION = 0.20
TARGET_FRACTION = 0.30
AFTERNOON_TARGET_FRACTION = 0.15
AFTERNOON_FROM = time(13, 30)
RUNGS = (0.10, 0.15)
HARD_FLAT = time(15, 10)
ENTRY_CUTOFF = time(14, 30)


@dataclass
class Trade:
    strategy: str
    date: str
    signal_time: str
    trigger: str
    spot: float
    expiry: str
    strikes: str
    entry_time: str
    entry_premium: float
    exit_time: str
    exit_premium: float
    exit_reason: str
    gross_pnl: float
    cost: float
    pnl: float


def _ms(value: Any) -> int:
    return int(round(value.timestamp() * 1000))


def _ist_time(ms: int) -> time:
    return datetime.fromtimestamp(ms / 1000, UTC).astimezone(IST).time()


def _ist_date(ms: int) -> date:
    return datetime.fromtimestamp(ms / 1000, UTC).astimezone(IST).date()


def _ist_iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, UTC).astimezone(IST).isoformat(timespec="minutes")


def _load_underlying(start: date, end: date) -> Dict[date, List[Dict[str, Any]]]:
    frame = pickle.load(PKL.open("rb"))
    by_day: Dict[date, List[Dict[str, Any]]] = defaultdict(list)
    for ts, row in frame.iterrows():
        ts = ts.astimezone(IST)
        day = ts.date()
        if not (start <= day <= end):
            continue
        clock = ts.time()
        if not (time(9, 15) <= clock <= time(15, 25)):
            continue
        by_day[day].append({
            "timestamp": ts.isoformat(),
            "open": float(row["Open"]),
            "high": float(row["High"]),
            "low": float(row["Low"]),
            "close": float(row["Close"]),
            "volume": float(row["Volume"]),
            "exchange": "NSE",
            "underlying_symbol": "NIFTY",
            "instrument_type": "index",
        })
    return dict(sorted(by_day.items()))


def _load_archive(start: date, end: date):
    """Return (contract lookup, premium-by-contract map, eligible expiries)."""
    uri = DB.resolve().as_uri() + "?mode=ro"
    con = sqlite3.connect(uri, uri=True)
    try:
        expiry_rows = con.execute(
            """
            select expiry_date, count(distinct strike)
            from contracts
            where underlying='NIFTY' and option_type in ('CE','PE')
              and strike between 24500 and 26000
            group by expiry_date
            having count(distinct strike) >= 10
            order by expiry_date
            """
        ).fetchall()
        expiries = [row[0] for row in expiry_rows]

        contracts: Dict[Tuple[str, str, float], int] = {}
        for cid, expiry, kind, strike in con.execute(
            """
            select contract_id, expiry_date, option_type, strike
            from contracts
            where underlying='NIFTY' and option_type in ('CE','PE')
              and strike between 20000 and 28000
            """
        ):
            contracts[(str(expiry), str(kind).upper(), float(strike))] = int(cid)

        t0 = _ms(datetime.combine(start, time(0), tzinfo=IST))
        t1 = _ms(datetime.combine(end + timedelta(days=1), time(0), tzinfo=IST))
        premiums: Dict[int, Dict[int, float]] = defaultdict(dict)
        placeholders = ",".join("?" for _ in expiries)
        for cid, stamp, close in con.execute(
            f"""
            select k.contract_id, k.timestamp_ms, k.close
            from candles k join contracts x using(contract_id)
            where x.underlying='NIFTY' and x.expiry_date in ({placeholders})
              and x.strike between 20000 and 28000
              and k.timestamp_ms between ? and ?
            """,
            [*expiries, t0, t1],
        ):
            price = float(close) if close is not None else 0.0
            if price > 0:
                premiums[int(cid)][int(stamp)] = price
        return contracts, premiums, expiries
    finally:
        con.close()


def _expiry_for(day: date, expiries: List[str]) -> Optional[str]:
    for expiry in expiries:
        if date.fromisoformat(expiry) > day:
            return expiry
    return None


def _target_fraction(entry_clock: time) -> float:
    return AFTERNOON_TARGET_FRACTION if entry_clock >= AFTERNOON_FROM else TARGET_FRACTION


def _run_one(
    key: str,
    engine: Any,
    by_day: Dict[date, List[Dict[str, Any]]],
    contracts: Dict[Tuple[str, str, float], int],
    premiums: Dict[int, Dict[int, float]],
    expiries: List[str],
) -> Tuple[List[Trade], Counter]:
    specs = {"lot_size": LOT, "strike_interval": STEP, "expiry_preference": "weekly"}
    trades: List[Trade] = []
    skipped: Counter = Counter()
    engines = {key: engine}

    for day, candles in by_day.items():
        first_signal = None
        for index, candle in enumerate(candles):
            result = engines[key].evaluate_signal(
                candles, index, specs, underlying_symbol="NIFTY"
            )
            if bool(getattr(result, "allowed", False)):
                first_signal = result
                break
        if first_signal is None:
            skipped["no_signal"] += 1
            continue

        signal_ms = _ms(datetime.fromisoformat(first_signal.trigger_time or candles[0]["timestamp"]))
        expiry = _expiry_for(day, expiries)
        if not expiry:
            skipped["no_expiry"] += 1
            continue

        leg_rows = []
        for leg in first_signal.legs:
            cid = contracts.get(
                (expiry, str(leg.kind).upper(), round(float(leg.strike), 2))
            )
            if cid is None or not premiums.get(cid):
                skipped["missing_contract"] += 1
                leg_rows = []
                break
            leg_rows.append((leg, cid))
        if len(leg_rows) != 2:
            continue

        (_, ce_id), (_, pe_id) = leg_rows
        common = sorted(set(premiums[ce_id]) & set(premiums[pe_id]))
        entry_ts = next(
            (ts for ts in common
             if ts > signal_ms and _ist_date(ts) == day
             and _ist_time(ts) <= ENTRY_CUTOFF),
            None,
        )
        if entry_ts is None:
            skipped["no_entry_quote"] += 1
            continue

        combined = lambda ts: premiums[ce_id][ts] + premiums[pe_id][ts]
        entry = round(combined(entry_ts), 2)
        entry_clock = _ist_time(entry_ts)
        target_fraction = _target_fraction(entry_clock)
        target = round(entry * (1.0 + target_fraction), 2)
        initial_stop = round(entry * (1.0 - STOP_FRACTION), 2)
        stop = initial_stop
        usable_rungs = [level for level in RUNGS if level < target_fraction - 1e-9]
        stage = 0

        exit_ts = None
        exit_premium = None
        reason = "eod-squareoff"
        for ts in common:
            if ts <= entry_ts:
                continue
            if _ist_date(ts) != day:
                break
            mark = round(combined(ts), 2)
            if mark <= stop:
                reason = "trailing-stop" if stop > entry + 1e-9 else "stop-loss"
                exit_ts, exit_premium = ts, mark
                break
            if mark >= target:
                reason = "target"
                exit_ts, exit_premium = ts, mark
                break
            while stage < len(usable_rungs) and mark >= entry * (1.0 + usable_rungs[stage]):
                stage += 1
            if stage > 0:
                stop = round(entry * (1.0 + usable_rungs[stage - 1]), 2)
            if _ist_time(ts) >= HARD_FLAT:
                exit_ts, exit_premium = ts, mark
                break
        if exit_ts is None:
            exit_ts = common[-1]
            exit_premium = round(combined(exit_ts), 2)

        gross = round((float(exit_premium) - entry) * LOT, 2)
        cost = round_trip_cost(
            key.replace("_", "-"), entry, float(exit_premium), lots=1, lot_size=LOT
        )
        pnl = round(gross - float(cost), 2)
        strikes = [float(leg.strike) for leg, _ in leg_rows]
        trades.append(Trade(
            strategy=key,
            date=day.isoformat(),
            signal_time=_ist_iso(signal_ms),
            trigger=str(getattr(first_signal, "trigger_source", "") or ""),
            spot=round(float(first_signal.entry_price), 2),
            expiry=expiry,
            strikes=",".join(f"{value:.0f}" for value in strikes),
            entry_time=_ist_iso(entry_ts),
            entry_premium=entry,
            exit_time=_ist_iso(exit_ts),
            exit_premium=round(float(exit_premium), 2),
            exit_reason=reason,
            gross_pnl=gross,
            cost=float(cost),
            pnl=pnl,
        ))
    return trades, skipped


def _summarize(trades: List[Trade]) -> Dict[str, Any]:
    if not trades:
        return {"trades": 0, "win_rate": 0.0, "net_pnl": 0.0}
    wins = [trade for trade in trades if trade.pnl > 0]
    losses = [trade for trade in trades if trade.pnl < 0]
    gross_profit = sum(trade.pnl for trade in wins)
    gross_loss = -sum(trade.pnl for trade in losses)
    equity = 0.0
    peak = 0.0
    max_dd = 0.0
    for trade in trades:
        equity += trade.pnl
        peak = max(peak, equity)
        max_dd = min(max_dd, equity - peak)
    return {
        "trades": len(trades),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": round(100.0 * len(wins) / len(trades), 2),
        "net_pnl": round(sum(trade.pnl for trade in trades), 2),
        "gross_pnl": round(sum(trade.gross_pnl for trade in trades), 2),
        "total_cost": round(sum(trade.cost for trade in trades), 2),
        "profit_factor": round(gross_profit / gross_loss, 2) if gross_loss else None,
        "avg_pnl": round(sum(trade.pnl for trade in trades) / len(trades), 2),
        "best": round(max(trade.pnl for trade in trades), 2),
        "worst": round(min(trade.pnl for trade in trades), 2),
        "max_drawdown": round(max_dd, 2),
        "exit_reasons": dict(Counter(trade.exit_reason for trade in trades)),
    }


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", default="2026-10-14")
    parser.add_argument("--end", default="2026-11-11")
    parser.add_argument(
        "--triggers", default="orb,pullback,momentum",
        help="comma-separated causal trigger chain (default matches live)",
    )
    args = parser.parse_args(argv)

    start = date.fromisoformat(args.start)
    end = date.fromisoformat(args.end)
    triggers = tuple(part.strip().lower() for part in args.triggers.split(",") if part.strip())

    by_day = _load_underlying(start, end)
    contracts, premiums, expiries = _load_archive(start, end)
    engines = {
        "long_straddle": LongStraddleStrategy(lot_size=LOT, trigger_strategies=triggers),
        "long_strangle": LongStrangleStrategy(lot_size=LOT, trigger_strategies=triggers),
    }

    all_trades: List[Trade] = []
    report: Dict[str, Any] = {
        "method": "real option candle close proxy; live-style 20/30-15% premium ladder; net of round_trip_cost",
        "window": {"start": start.isoformat(), "end": end.isoformat(),
                   "sessions": len(by_day)},
        "triggers": list(triggers),
        "expiries_used": expiries,
        "caveats": [
            "Option candle closes are used as entry/mark/exit premium; live uses executable ask/bid.",
            "One first signal per strategy per session; live cooldowns/pyramiding are not replayed.",
            "Archive expiry coverage is partial, so DTE is not always the weekly 0-DTE choice.",
            "Research result only; not an executable P&L claim.",
        ],
        "results": {},
    }

    for key, engine in engines.items():
        trades, skipped = _run_one(key, engine, by_day, contracts, premiums, expiries)
        report["results"][key] = {
            **_summarize(trades),
            "skipped": dict(skipped),
        }
        all_trades.extend(trades)
        summary = report["results"][key]
        print(
            f"{key:<14} trades={summary.get('trades', 0):<3} "
            f"win={summary.get('win_rate', 0):>5}% "
            f"net={summary.get('net_pnl', 0):>+12,.2f} "
            f"PF={summary.get('profit_factor')} "
            f"maxDD={summary.get('max_drawdown', 0):>+11,.2f} "
            f"exits={summary.get('exit_reasons', {})}"
        )

    OUT_JSON.write_text(json.dumps(report, indent=2), encoding="utf-8")
    with OUT_CSV.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(Trade.__dataclass_fields__))
        writer.writeheader()
        for trade in all_trades:
            writer.writerow(asdict(trade))
    print(f"\nWrote {OUT_JSON.relative_to(ROOT)} and {OUT_CSV.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
