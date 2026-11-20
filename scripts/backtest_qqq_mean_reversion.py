"""Backtest the QQQ daily RSI(3) + EMA(100) + 10-day VWAP mean-reversion rules.

Rules implemented exactly as specified:

* RSI(3) closes below 22.
* EMA(100) is higher than its value 5 trading days ago.
* Close is at least 1% below the 10-day rolling VWAP.
* Enter at the next session's open.
* Exit at the close or next open once RSI(3) closes above 55.
* One position at a time, fixed $100,000 notional, no compounding.

The script is deliberately standalone and causal: every signal uses data up to
and including the signal day's close, and the entry is the following session's
open.  ``--grid`` runs the plausible RSI/VWAP/exit/adjustment variants so the
reported numbers can be compared with the claimed 215-trade / PF 3.94 result
instead of silently picking one definition.

    python scripts/backtest_qqq_mean_reversion.py
    python scripts/backtest_qqq_mean_reversion.py --grid
    python scripts/backtest_qqq_mean_reversion.py --exit-timing same_close
    python scripts/backtest_qqq_mean_reversion.py --cost-bps 2

Data is cached at ``output/qqq_daily.csv``.  Use ``--refresh`` to re-download.

DISCLAIMER: Educational purposes only. Not financial advice.
"""

from __future__ import annotations

import argparse
import json
import math
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CACHE = ROOT / "output" / "qqq_daily.csv"
DEFAULT_JSON = ROOT / "output" / "qqq_mean_reversion_backtest.json"
DEFAULT_TRADES = ROOT / "output" / "qqq_mean_reversion_trades.csv"

CAPITAL = 100_000.0
START = "1999-03-01"
END = "2026-11-14"

# The numbers in the user's brief, used only to score variant distance.
CLAIMED = {
    "trades": 215,
    "win_rate": 78.6,
    "profit_factor": 3.94,
    "avg_trade_return_pct": 1.06,
    "max_drawdown_pct_of_capital": 10.94,
    "net_pnl": 500_000.0,
}

# The brief's RSI-only comparison.  Average return and net P&L were not given.
CLAIMED_RSI_ONLY = {
    "trades": 340,
    "win_rate": 72.4,
    "profit_factor": 2.19,
    "avg_trade_return_pct": None,
    "max_drawdown_pct_of_capital": 22.7,
    "net_pnl": None,
}


# ---------------------------------------------------------------------------
# data
# ---------------------------------------------------------------------------

def _flatten_columns(frame: pd.DataFrame) -> pd.DataFrame:
    if isinstance(frame.columns, pd.MultiIndex):
        frame = frame.copy()
        frame.columns = [str(column[0]) for column in frame.columns]
    return frame


def download_qqq(start: str = START, end: str = END) -> pd.DataFrame:
    """Download raw QQQ daily bars plus the adjusted-close factor.

    ``auto_adjust=False`` keeps both the raw OHLC and ``Adj Close`` so the same
    cache can serve the adjusted and unadjusted variants without a second
    network call.
    """
    import yfinance as yf

    raw = yf.download(
        "QQQ", start=start, end=end, interval="1d",
        auto_adjust=False, progress=False, threads=False,
    )
    raw = _flatten_columns(raw)
    raw = raw.rename_axis("Date")
    required = {"Open", "High", "Low", "Close", "Volume"}
    missing = required - set(raw.columns)
    if missing:
        raise RuntimeError(f"QQQ download missing columns: {sorted(missing)}")
    if "Adj Close" not in raw.columns:
        raw["Adj Close"] = raw["Close"]
    raw = raw[["Open", "High", "Low", "Close", "Adj Close", "Volume"]]
    raw = raw.dropna(subset=["Open", "High", "Low", "Close", "Volume"])
    raw = raw[raw["Volume"] > 0]
    raw.index = pd.to_datetime(raw.index).tz_localize(None)
    return raw


def load_qqq(cache_path: Path = DEFAULT_CACHE, *, refresh: bool = False,
             start: str = START, end: str = END) -> pd.DataFrame:
    if cache_path.exists() and not refresh:
        frame = pd.read_csv(cache_path, parse_dates=["Date"], index_col="Date")
        frame = _flatten_columns(frame)
        if "Adj Close" not in frame.columns:
            frame["Adj Close"] = frame["Close"]
        frame = frame[["Open", "High", "Low", "Close", "Adj Close", "Volume"]]
        frame = frame.dropna(subset=["Open", "High", "Low", "Close", "Volume"])
        frame = frame[frame["Volume"] > 0]
        frame.index = pd.to_datetime(frame.index).tz_localize(None)
        return frame
    frame = download_qqq(start, end)
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    frame.to_csv(cache_path, index_label="Date")
    return frame


def adjusted_bars(frame: pd.DataFrame, *, adjusted: bool) -> pd.DataFrame:
    """Return OHLCV bars, optionally scaled by the adjusted-close factor."""
    bars = frame[["Open", "High", "Low", "Close", "Volume"]].copy()
    if adjusted:
        factor = (frame["Adj Close"] / frame["Close"]).replace([np.inf, -np.inf], np.nan)
        factor = factor.ffill().fillna(1.0)
        for column in ("Open", "High", "Low", "Close"):
            bars[column] = bars[column] * factor
    return bars


# ---------------------------------------------------------------------------
# indicators
# ---------------------------------------------------------------------------

def rsi_wilder(close: pd.Series, period: int = 3) -> pd.Series:
    """Wilder's RSI, seeded with the first ``period`` simple average."""
    values = close.to_numpy(dtype=float)
    out = np.full(len(values), np.nan, dtype=float)
    if period <= 0 or len(values) <= period:
        return pd.Series(out, index=close.index)
    deltas = np.diff(values)
    avg_gain = float(np.mean(np.clip(deltas[:period], 0.0, None)))
    avg_loss = float(np.mean(np.clip(-deltas[:period], 0.0, None)))
    out[period] = 100.0 if avg_loss == 0 else 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)
    for index in range(period + 1, len(values)):
        delta = deltas[index - 1]
        gain = max(delta, 0.0)
        loss = max(-delta, 0.0)
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period
        out[index] = 100.0 if avg_loss == 0 else 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)
    return pd.Series(out, index=close.index)


def rsi_sma(close: pd.Series, period: int = 3) -> pd.Series:
    """Simple-average RSI (the common charting-platform default)."""
    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = (-delta).clip(lower=0.0)
    avg_gain = gain.rolling(period).mean()
    avg_loss = loss.rolling(period).mean()
    rs = avg_gain / avg_loss.replace(0.0, np.nan)
    out = 100.0 - 100.0 / (1.0 + rs)
    out = out.mask((avg_loss == 0) & (avg_gain > 0), 100.0)
    out = out.mask((avg_gain == 0) & (avg_loss > 0), 0.0)
    return out


def ema(close: pd.Series, period: int = 100) -> pd.Series:
    return close.ewm(span=period, adjust=False).mean()


def rolling_vwap(bars: pd.DataFrame, period: int = 10,
                 price: str = "typical") -> pd.Series:
    if price == "close":
        value = bars["Close"]
    else:
        value = (bars["High"] + bars["Low"] + bars["Close"]) / 3.0
    volume = bars["Volume"]
    return (value * volume).rolling(period).sum() / volume.rolling(period).sum()


# ---------------------------------------------------------------------------
# signals and execution
# ---------------------------------------------------------------------------

def build_signals(bars: pd.DataFrame, *, rsi_method: str = "wilder",
                  vwap_price: str = "typical", ema_period: int = 100,
                  ema_lookback: int = 5, rsi_entry: float = 22.0,
                  rsi_exit: float = 55.0, discount: float = 0.01,
                  use_trend: bool = True, use_vwap: bool = True) -> pd.DataFrame:
    rsi = rsi_wilder(bars["Close"], 3) if rsi_method == "wilder" else rsi_sma(bars["Close"], 3)
    trend = ema(bars["Close"], ema_period)
    trend_rising = trend > trend.shift(ema_lookback)
    vwap = rolling_vwap(bars, 10, vwap_price)
    value_discount = bars["Close"] <= vwap * (1.0 - discount)

    enough_history = rsi.notna()
    if use_trend:
        enough_history = enough_history & trend.notna() & trend.shift(ema_lookback).notna()
    if use_vwap:
        enough_history = enough_history & vwap.notna()
    entry = enough_history & (rsi < rsi_entry)
    if use_trend:
        entry = entry & trend_rising
    if use_vwap:
        entry = entry & value_discount
    exit_ = rsi.notna() & (rsi > rsi_exit)
    return pd.DataFrame({
        "rsi": rsi,
        "ema": trend,
        "ema_rising": trend_rising,
        "vwap": vwap,
        "value_discount": value_discount,
        "entry": entry.fillna(False).astype(bool),
        "exit": exit_.fillna(False).astype(bool),
    })


@dataclass
class Trade:
    signal_date: str
    entry_date: str
    exit_date: str
    entry_price: float
    exit_price: float
    return_pct: float
    pnl: float
    bars_held: int
    exit_reason: str
    entry_rsi: float
    entry_vwap_discount_pct: float


def run_backtest(bars: pd.DataFrame, signals: pd.DataFrame, *,
                 exit_timing: str = "next_open", cost_bps: float = 0.0,
                 capital: float = CAPITAL) -> List[Trade]:
    """Simulate one fixed-notional position at a time.

    Entry is always the next session's open after a completed signal close.
    ``exit_timing="next_open"`` is the strictly causal default; ``same_close``
    assumes an MOC fill at the signal day's close and is reported as an upper
    bound.
    """
    trades: List[Trade] = []
    position: Optional[Dict[str, Any]] = None
    cost = max(0.0, float(cost_bps)) / 10_000.0

    def close_position(exit_index: int, exit_price: float, reason: str) -> None:
        nonlocal position
        if position is None:
            return
        entry_effective = position["entry_price"] * (1.0 + cost)
        exit_effective = exit_price * (1.0 - cost)
        return_pct = (exit_effective / entry_effective - 1.0) * 100.0
        trades.append(Trade(
            signal_date=position["signal_date"],
            entry_date=position["entry_date"],
            exit_date=bars.index[exit_index].date().isoformat(),
            entry_price=round(position["entry_price"], 4),
            exit_price=round(exit_price, 4),
            return_pct=round(return_pct, 6),
            pnl=round(capital * (exit_effective / entry_effective - 1.0), 2),
            bars_held=exit_index - position["entry_index"],
            exit_reason=reason,
            entry_rsi=round(position["entry_rsi"], 4),
            entry_vwap_discount_pct=round(position["entry_vwap_discount_pct"], 4),
        ))
        position = None

    for index in range(len(bars)):
        if position is None:
            if not bool(signals["entry"].iloc[index]):
                continue
            if index + 1 >= len(bars):
                continue
            entry_index = index + 1
            entry_price = float(bars["Open"].iloc[entry_index])
            if not math.isfinite(entry_price) or entry_price <= 0:
                continue
            vwap = float(signals["vwap"].iloc[index])
            close = float(bars["Close"].iloc[index])
            position = {
                "signal_date": bars.index[index].date().isoformat(),
                "entry_date": bars.index[entry_index].date().isoformat(),
                "entry_index": entry_index,
                "entry_price": entry_price,
                "entry_rsi": float(signals["rsi"].iloc[index]),
                "entry_vwap_discount_pct": (close / vwap - 1.0) * 100.0,
            }
            continue

        if not bool(signals["exit"].iloc[index]):
            continue
        if exit_timing == "same_close":
            close_position(index, float(bars["Close"].iloc[index]), "rsi_exit")
        elif index + 1 < len(bars):
            close_position(index + 1, float(bars["Open"].iloc[index + 1]), "rsi_exit")
        else:
            close_position(index, float(bars["Close"].iloc[index]), "end_of_data")

    if position is not None:
        close_position(len(bars) - 1, float(bars["Close"].iloc[-1]), "end_of_data")
    return trades


# ---------------------------------------------------------------------------
# metrics
# ---------------------------------------------------------------------------

def summarize(trades: Sequence[Trade], *, capital: float = CAPITAL) -> Dict[str, Any]:
    pnls = np.array([trade.pnl for trade in trades], dtype=float)
    returns = np.array([trade.return_pct for trade in trades], dtype=float)
    count = len(pnls)
    if count == 0:
        return {
            "trades": 0, "wins": 0, "losses": 0, "win_rate": 0.0,
            "gross_profit": 0.0, "gross_loss": 0.0, "profit_factor": 0.0,
            "avg_trade_return_pct": 0.0, "net_pnl": 0.0,
            "max_drawdown": 0.0, "max_drawdown_pct_of_capital": 0.0,
            "max_drawdown_pct_of_peak": 0.0, "avg_bars_held": 0.0,
        }
    wins = pnls[pnls > 0]
    losses = pnls[pnls <= 0]
    gross_profit = float(wins.sum())
    gross_loss = float(-losses.sum())
    equity = np.concatenate(([capital], capital + np.cumsum(pnls)))
    peaks = np.maximum.accumulate(equity)
    drawdowns = equity - peaks
    max_drawdown = float(drawdowns.min())
    peak_at_dd = float(peaks[int(np.argmin(drawdowns))])
    return {
        "trades": count,
        "wins": int(len(wins)),
        "losses": int(len(losses)),
        "win_rate": float(len(wins) / count * 100.0),
        "gross_profit": round(gross_profit, 2),
        "gross_loss": round(gross_loss, 2),
        "profit_factor": (round(gross_profit / gross_loss, 4)
                          if gross_loss else float("inf")),
        "avg_trade_return_pct": float(returns.mean()),
        "net_pnl": round(float(pnls.sum()), 2),
        "max_drawdown": round(max_drawdown, 2),
        "max_drawdown_pct_of_capital": round(abs(max_drawdown) / capital * 100.0, 4),
        "max_drawdown_pct_of_peak": (round(abs(max_drawdown) / peak_at_dd * 100.0, 4)
                                     if peak_at_dd else 0.0),
        "avg_bars_held": round(float(np.mean([trade.bars_held for trade in trades])), 2),
    }


def yearly_breakdown(trades: Sequence[Trade]) -> Dict[str, Dict[str, Any]]:
    grouped: Dict[str, List[Trade]] = {}
    for trade in trades:
        year = trade.entry_date[:4]
        grouped.setdefault(year, []).append(trade)
    out: Dict[str, Dict[str, Any]] = {}
    for year, group in sorted(grouped.items()):
        pnls = [trade.pnl for trade in group]
        wins = [value for value in pnls if value > 0]
        out[year] = {
            "trades": len(group),
            "win_rate": round(len(wins) / len(group) * 100.0, 2),
            "net_pnl": round(sum(pnls), 2),
        }
    return out


def exit_breakdown(trades: Sequence[Trade]) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    for trade in trades:
        counts[trade.exit_reason] = counts.get(trade.exit_reason, 0) + 1
    return dict(sorted(counts.items()))


# ---------------------------------------------------------------------------
# reporting
# ---------------------------------------------------------------------------

def _pf(value: float) -> str:
    return "inf" if value == float("inf") else f"{value:.2f}"


def format_report(result: Dict[str, Any]) -> str:
    stats = result["stats"]
    config = result["config"]
    baseline = config.get("baseline", "full")
    rules = ("RSI(3)<22 only (filter-impact baseline)" if baseline == "rsi_only"
             else "RSI(3)<22, EMA(100)>EMA(100)[-5], close<=VWAP10*0.99")
    lines = [
        "QQQ MEAN-REVERSION BACKTEST (RSI3 + EMA100 + 10d VWAP)",
        "=" * 92,
        f"Data       : {result['data']['first']} .. {result['data']['last']} "
        f"({result['data']['bars']} bars)",
        f"Variant    : RSI={config['rsi_method']} | VWAP={config['vwap_price']} | "
        f"exit={config['exit_timing']} | adjusted={config['adjusted']} | "
        f"cost={config['cost_bps']:.1f} bps/side",
        f"Rules      : {rules}; entry next open, exit RSI(3)>55",
        "",
        f"{'Trades':>8}{'Win%':>8}{'PF':>7}{'Avg%':>9}{'Net $':>13}"
        f"{'MaxDD $':>12}{'MaxDD%':>9}{'AvgBars':>9}",
        "-" * 92,
        f"{stats['trades']:>8}{stats['win_rate']:>7.1f}%{_pf(stats['profit_factor']):>7}"
        f"{stats['avg_trade_return_pct']:>9.2f}{stats['net_pnl']:>13,.0f}"
        f"{stats['max_drawdown']:>12,.0f}{stats['max_drawdown_pct_of_capital']:>8.1f}%"
        f"{stats['avg_bars_held']:>9.1f}",
        "-" * 92,
        f"Gross profit ${stats['gross_profit']:,.0f} | gross loss ${stats['gross_loss']:,.0f} | "
        f"max DD {stats['max_drawdown_pct_of_peak']:.2f}% of peak equity",
        f"Exits: {result['exits']}",
        "",
        "CLAIMED VS THIS RUN",
        "-" * 92,
    ]
    claimed = result["claimed"]
    for key, label, fmt in (
        ("trades", "Trades", "{:.0f}"),
        ("win_rate", "Win rate %", "{:.1f}"),
        ("profit_factor", "Profit factor", "{:.2f}"),
        ("avg_trade_return_pct", "Avg trade %", "{:.2f}"),
        ("max_drawdown_pct_of_capital", "Max DD % of $100k", "{:.2f}"),
        ("net_pnl", "Net P&L $", "{:,.0f}"),
    ):
        claimed_value = claimed.get(key)
        if claimed_value is None:
            continue
        lines.append(f"{label:<20} claimed={fmt.format(claimed_value):>12}   "
                     f"actual={fmt.format(stats[key]):>12}")
    lines.append("")
    lines.append("BY YEAR")
    lines.append("-" * 92)
    for year, item in result["by_year"].items():
        lines.append(f"{year}  trades={item['trades']:>3}  win={item['win_rate']:>5.1f}%  "
                     f"net=${item['net_pnl']:>10,.0f}")
    return "\n".join(lines)


def run_variant(bars: pd.DataFrame, *, rsi_method: str, vwap_price: str,
                exit_timing: str, cost_bps: float, capital: float,
                use_trend: bool = True, use_vwap: bool = True) -> Dict[str, Any]:
    signals = build_signals(bars, rsi_method=rsi_method, vwap_price=vwap_price,
                            use_trend=use_trend, use_vwap=use_vwap)
    trades = run_backtest(bars, signals, exit_timing=exit_timing,
                          cost_bps=cost_bps, capital=capital)
    return {"signals": signals, "trades": trades, "stats": summarize(trades, capital=capital)}


def variant_distance(stats: Dict[str, Any]) -> float:
    """Relative distance from the user's claimed metrics (lower is closer)."""
    total = 0.0
    for key, claimed in CLAIMED.items():
        actual = stats.get(key, 0.0)
        if actual == float("inf"):
            actual = claimed * 10.0
        total += abs(float(actual) - float(claimed)) / max(abs(float(claimed)), 1.0)
    return total


def run_grid(bars: pd.DataFrame, *, capital: float = CAPITAL) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for rsi_method in ("wilder", "sma"):
        for vwap_price in ("typical", "close"):
            for exit_timing in ("next_open", "same_close"):
                for adjusted in (True, False):
                    variant_bars = adjusted_bars(bars, adjusted=adjusted)
                    result = run_variant(
                        variant_bars, rsi_method=rsi_method, vwap_price=vwap_price,
                        exit_timing=exit_timing, cost_bps=0.0, capital=capital)
                    rows.append({
                        "rsi_method": rsi_method,
                        "vwap_price": vwap_price,
                        "exit_timing": exit_timing,
                        "adjusted": adjusted,
                        **result["stats"],
                    })
    rows.sort(key=lambda row: variant_distance(row))
    return rows


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", default=START)
    parser.add_argument("--end", default=END)
    parser.add_argument("--cache", type=Path, default=DEFAULT_CACHE)
    parser.add_argument("--refresh", action="store_true")
    parser.add_argument("--rsi-method", choices=("wilder", "sma"), default="wilder")
    parser.add_argument("--vwap-price", choices=("typical", "close"), default="typical")
    parser.add_argument("--exit-timing", choices=("next_open", "same_close"),
                        default="next_open")
    parser.add_argument("--adjusted", action="store_true",
                        help="use split/dividend-adjusted OHLC (default: raw QQQ prices)")
    parser.add_argument("--baseline", choices=("full", "rsi_only"), default="full",
                        help="full combo or the RSI-only filter-impact baseline")
    parser.add_argument("--cost-bps", type=float, default=0.0,
                        help="round-trip cost per side in basis points")
    parser.add_argument("--capital", type=float, default=CAPITAL)
    parser.add_argument("--grid", action="store_true",
                        help="run all RSI/VWAP/exit/adjustment variants")
    parser.add_argument("--json", type=Path, default=DEFAULT_JSON)
    parser.add_argument("--trades-csv", type=Path, default=DEFAULT_TRADES)
    args = parser.parse_args(argv)

    frame = load_qqq(args.cache, refresh=args.refresh,
                     start=args.start, end=args.end)
    bars = adjusted_bars(frame, adjusted=args.adjusted)

    if args.grid:
        rows = run_grid(frame, capital=args.capital)
        print("VARIANT GRID (closest to claimed metrics first)")
        print("=" * 128)
        print(f"{'RSI':<8}{'VWAP':<10}{'Exit':<12}{'Adj':<6}{'Trades':>7}"
              f"{'Win%':>8}{'PF':>7}{'Avg%':>8}{'Net $':>12}{'DD $':>10}"
              f"{'DD%':>7}{'Dist':>8}")
        print("-" * 128)
        for row in rows:
            print(f"{row['rsi_method']:<8}{row['vwap_price']:<10}"
                  f"{row['exit_timing']:<12}{str(row['adjusted']):<6}"
                  f"{row['trades']:>7}{row['win_rate']:>7.1f}%"
                  f"{_pf(row['profit_factor']):>7}{row['avg_trade_return_pct']:>8.2f}"
                  f"{row['net_pnl']:>12,.0f}{row['max_drawdown']:>10,.0f}"
                  f"{row['max_drawdown_pct_of_capital']:>6.1f}%"
                  f"{variant_distance(row):>8.2f}")
        if args.json:
            args.json.parent.mkdir(parents=True, exist_ok=True)
            args.json.write_text(json.dumps(rows, indent=2, default=str), encoding="utf-8")
            print(f"\nSaved grid JSON: {args.json}")
        return 0

    result = run_variant(
        bars, rsi_method=args.rsi_method, vwap_price=args.vwap_price,
        exit_timing=args.exit_timing, cost_bps=args.cost_bps,
        capital=args.capital,
        use_trend=args.baseline != "rsi_only",
        use_vwap=args.baseline != "rsi_only")
    trades: List[Trade] = result["trades"]
    claimed = CLAIMED_RSI_ONLY if args.baseline == "rsi_only" else CLAIMED
    report = {
        "config": {
            "start": args.start, "end": args.end,
            "rsi_method": args.rsi_method, "vwap_price": args.vwap_price,
            "exit_timing": args.exit_timing, "adjusted": args.adjusted,
            "baseline": args.baseline,
            "cost_bps": args.cost_bps, "capital": args.capital,
        },
        "data": {
            "first": bars.index[0].date().isoformat(),
            "last": bars.index[-1].date().isoformat(),
            "bars": len(bars),
        },
        "stats": result["stats"],
        "claimed": claimed,
        "by_year": yearly_breakdown(trades),
        "exits": exit_breakdown(trades),
        "trades": [asdict(trade) for trade in trades],
    }
    print(format_report(report))

    if args.trades_csv:
        args.trades_csv.parent.mkdir(parents=True, exist_ok=True)
        pd.DataFrame(report["trades"]).to_csv(args.trades_csv, index=False)
        print(f"\nSaved trades: {args.trades_csv}")
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
        print(f"Saved JSON: {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
